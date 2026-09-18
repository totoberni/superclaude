"""/notebook skill — kernel persistence + execute.

Persistent jupyter_client.KernelManager keyed by (project, $CLAUDE_SESSION_ID).
Connection file at ~/.cache/notebook-skill/kernels/<project>-<session>.json.
Liveness via client.kernel_info(timeout=2) ZMQ probe.

V1.1 — addresses example-project-review-of-skill-v1.md issues:
  - V1-B3: kernel_name auto-resolves to project venv kernelspec.
  - V1-H1: kernel detached so it survives the CLI process exit.
  - V1-H2: `--cells "id1,id2"` parser bug fixed.
  - V1-H11: chmod 600 BEFORE start_kernel (umask) to close brief race window.
  - V1-H12: shell+iopub channel coordination.
  - V1-L3: `cmd_reset_kernel` requires notebook arg.

V1.2 — addresses example-project-review-of-skill-v2.md issues:
  - V2-C1: `_execute_cell` is now SINGLE-THREADED with a kernel-liveness
           watchdog. No ZMQ thread-safety violation. Dead kernel is detected
           in <30 s rather than after `cell_timeout`.
  - V2-H3: `_session_id()` falls back to `os.getsid(0)` (stable session leader)
           instead of `getppid()` which churns per shell invocation.
  - V2-H4 + V2-M5: dropped the global `subprocess.Popen` monkey-patch. Kernel
           is started via `KernelManager.start_kernel(start_new_session=True,
           stdout=<log_fd>, stderr=<log_fd>)` directly. The log file at
           `~/.cache/notebook-skill/kernels/<project>-<session>.log` keeps stdio
           pipe-write-EPIPE-safe AND captures kernel diagnostics for debugging.
  - V2-L6: `km = None` initialised before the try block.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import nbformat

from nb_io import IS_WINDOWS, atomic_write_ipynb, canonical_repo_root, notebook_lock

try:
    import psutil
except ImportError:
    psutil = None

# NB_SKILL_CACHE_DIR: override per test e ambienti isolati. Il default resta
# corto (`~/.cache/...`) perche' su Windows i percorsi lunghi non sono abilitati.
CACHE_DIR = Path(os.environ.get("NB_SKILL_CACHE_DIR")
                 or Path.home() / ".cache" / "notebook-skill" / "kernels")

# Windows: console propria e nascosta + nuovo process group. Cosi' il kernel
# sopravvive alla chiusura della console della CLI e non riceve i suoi Ctrl+C.
# DETACHED_PROCESS non viene aggiunto di proposito: renderebbe CREATE_NO_WINDOW
# inefficace (documentazione Win32) e ogni sottoprocesso console del kernel
# (`!pip ...`) aprirebbe una finestra visibile.
_WIN_CREATE_NO_WINDOW = 0x08000000
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WIN_DETACH_FLAGS = _WIN_CREATE_NO_WINDOW | _WIN_CREATE_NEW_PROCESS_GROUP

# V2-C1: default heartbeat budget — if no iopub message arrives for this long
# during cell execution, probe the kernel via ZMQ kernel_info; raise on dead
# kernel. V3-X3: now overridable via `nb execute --iopub-heartbeat-timeout`
# for long Aer / Monte Carlo simulations that emit no intermediate iopub.
_HEARTBEAT_TIMEOUT = 30.0


def _project_name(notebook: Path) -> str:
    try:
        root = canonical_repo_root(notebook.parent)
        return root.name
    except RuntimeError:
        return notebook.parent.name


def _project_root(notebook: Path) -> Path:
    try:
        return canonical_repo_root(notebook.parent)
    except RuntimeError:
        return notebook.parent


def _session_id(project_root: Path | None = None) -> str:
    """V2-H3: prefer $CLAUDE_SESSION_ID, then `os.getsid(0)` (stable session
    leader across child invocations within the same shell session). Avoids
    `getppid()` cache-key churn when the parent process changes per call.

    Port Windows: l'ordine e' $CLAUDE_SESSION_ID, poi $CLAUDE_CODE_SESSION_ID
    (quella che Claude Code esporta davvero ai comandi), poi un fallback.
    Su Windows non esiste `os.getsid` e `os.getppid()` cambia a ogni chiamata
    (ogni comando passa da una shell nuova), quindi il fallback e' una chiave
    PER PROGETTO: `local-<8 hex dello sha1 della radice del repo>`. Conseguenza
    documentata: fuori da Claude Code tutte le shell condividono un solo kernel
    per progetto.
    """
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        sid = os.environ.get(var)
        if sid:
            # Solo caratteri sicuri per un nome di file; lunghezza limitata.
            return re.sub(r"[^A-Za-z0-9_-]", "_", sid)[:40]
    if not IS_WINDOWS:
        try:
            return f"sid-{os.getsid(0)}"
        except OSError:
            pass
    root = os.path.normcase(os.path.abspath(str(project_root or Path.cwd())))
    return "local-" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]


def _safe_project(project: str) -> str:
    # Nome di file corto e senza caratteri riservati (MAX_PATH = 260).
    return re.sub(r"[^A-Za-z0-9._-]", "_", project)[:40] or "project"


def _connection_file(project: str, session: str) -> Path:
    return CACHE_DIR / f"{_safe_project(project)}-{session}.json"


def _log_file(project: str, session: str) -> Path:
    return CACHE_DIR / f"{_safe_project(project)}-{session}.log"


def _meta_file(project: str, session: str) -> Path:
    """Sidecar con pid del kernel e handle dell'evento di interrupt (Windows)."""
    return CACHE_DIR / f"{_safe_project(project)}-{session}.meta"


def _read_meta(project: str, session: str) -> dict:
    try:
        return json.loads(_meta_file(project, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        # Su Windows chmod tocca solo il flag read-only: la riservatezza e'
        # gia' data dalle ACL NTFS di %USERPROFILE%.
        os.chmod(CACHE_DIR, 0o700)


def _use_selector_loop() -> None:
    """Windows: pyzmq/tornado richiedono il selector loop; con il Proactor di
    default ogni invocazione della CLI stamperebbe un RuntimeWarning."""
    if not IS_WINDOWS:
        return
    import asyncio
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@contextlib.contextmanager
def _windows_detached_launch():
    """Aggiunge i flag di detach al Popen usato da `jupyter_client.launcher`.

    `launch_kernel(independent=True)` SOVRASCRIVE `creationflags` con il solo
    CREATE_NEW_PROCESS_GROUP, quindi i flag non si possono passare come kwargs.
    La sostituzione riguarda il solo attributo `Popen` di quel modulo e dura
    quanto `start_kernel` (nessun monkey-patch globale di `subprocess`, V2-H4).
    Inoltre l'ereditarieta' degli handle viene ristretta: il launcher usa
    `close_fds=False`, cioe' il kernel erediterebbe OGNI handle ereditabile
    della CLI, comprese le pipe stdout/stderr create da PowerShell o dal tool
    dell'agente, e chi attende la chiusura di quelle pipe resterebbe bloccato
    finche' il kernel vive. Con `handle_list` il kernel eredita soltanto i suoi
    tre handle standard (stdin, log, log) e l'evento di interrupt.
    """
    if not IS_WINDOWS:
        yield
        return
    import msvcrt
    import subprocess

    from jupyter_client import launcher
    for fd in (0, 1, 2):
        try:
            os.set_handle_inheritable(msvcrt.get_osfhandle(fd), False)
        except (OSError, ValueError):
            pass
    orig = launcher.Popen

    def _popen(cmd, **kw):
        kw["creationflags"] = kw.get("creationflags", 0) | _WIN_DETACH_FLAGS
        keep = []
        ev = (kw.get("env") or {}).get("JPY_INTERRUPT_EVENT")
        if ev:
            keep.append(int(ev))
        si = kw.get("startupinfo") or subprocess.STARTUPINFO()
        si.lpAttributeList = {"handle_list": keep}
        kw["startupinfo"] = si
        kw["close_fds"] = True  # con handle_list: eredita solo la lista + stdio
        return subprocess.Popen(cmd, **kw)

    launcher.Popen = _popen
    try:
        yield
    finally:
        launcher.Popen = orig


def resolve_kernel_name(notebook: Path, override: str | None = None) -> str:
    """Resolve kernelspec to use, in priority order:

    1. Explicit `--kernel-name` arg.
    2. `<dir>/.notebook/kernel_name` file.
    3. `python3` (fallback, with WARN).
    """
    if override:
        return override
    cfg = notebook.parent / ".notebook" / "kernel_name"
    if cfg.exists():
        name = cfg.read_text(encoding="utf-8").strip()
        if name:
            return name
    print(f"[notebook] WARNING: no `.notebook/kernel_name` found; falling back to system "
          "`python3` kernelspec. Run `nb init` to register the project venv.",
          file=sys.stderr)
    return "python3"


def _kernel_alive(client, timeout: float = 2.0) -> bool:
    """ZMQ kernel_info round-trip — only authoritative cross-process check."""
    try:
        client.kernel_info(reply=True, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_or_start_kernel(project: str, session: str, *, kernel_name: str = "python3",
                       warm_timeout: float = 60.0):
    """Return (km, client) for the project's persistent kernel.

    Reuses if connection file exists and `kernel_info` succeeds; otherwise
    cleans stale state and starts fresh. Kernel is detached via
    `start_new_session=True` and stdio redirected to a per-session log file
    (V1-H1 + V2-H4 + V2-M5).
    """
    _ensure_cache_dir()
    _use_selector_loop()
    from jupyter_client import KernelManager
    cf = _connection_file(project, session)
    log = _log_file(project, session)
    km = None  # V2-L6: bind before try so the cleanup path can reference it.
    if cf.exists():
        client_to_clean = None
        try:
            km = KernelManager(connection_file=str(cf), kernel_name=kernel_name)
            km.load_connection_file()
            client = km.client()
            client.start_channels()
            if _kernel_alive(client):
                return km, client
            client_to_clean = client
        except Exception:  # noqa: BLE001
            pass
        print(f"[notebook] stale kernel for {project}; respawning. "
              "WARNING: in-memory variables LOST. Re-run upstream cells if needed.",
              file=sys.stderr)
        try:
            if client_to_clean is not None:
                client_to_clean.stop_channels()
        except Exception:  # noqa: BLE001
            pass
        try:
            if km is not None:
                km.cleanup_resources(restart=False)
        except Exception:  # noqa: BLE001
            pass
        # Il kernel puo' essere vivo ma bloccato: niente orfani (solo psutil).
        _kill_tree(_kernel_process(project, session))
        cf.unlink(missing_ok=True)  # belt-and-braces (jupyter_client #941)
        km = None  # reset for the fresh-spawn path below

    # V1-H11: tighten umask BEFORE start_kernel so connection file is born
    # chmod 600 (zero race window). Also handles the log file.
    old_umask = os.umask(0o077)
    try:
        # V2-H4 + V2-M5: open log fd at chmod 600 BEFORE handing to start_kernel.
        # No global Popen monkey-patch.
        log_fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            km = KernelManager(connection_file=str(cf), kernel_name=kernel_name)
            # `start_new_session=True` makes the kernel a session leader so it
            # survives the CLI parent's exit. `stdout=stderr=log_fd` keeps the
            # kernel's pipes open even after parent exits (avoids EPIPE on
            # write) and captures diagnostics for debugging.
            if IS_WINDOWS:
                # `independent=True`: senza, il launcher passa al kernel un
                # handle del processo padre (JPY_PARENT_PID) e ipykernel si
                # spegne appena la CLI termina.
                with _windows_detached_launch():
                    km.start_kernel(independent=True, stdout=log_fd, stderr=log_fd)
            else:
                km.start_kernel(
                    start_new_session=True,
                    stdout=log_fd,
                    stderr=log_fd,
                )
        finally:
            # The kernel inherits the FD via dup2; the parent can close.
            try:
                os.close(log_fd)
            except OSError:
                pass
    finally:
        os.umask(old_umask)
    if not IS_WINDOWS:
        try:
            os.chmod(cf, 0o600)
        except OSError:
            pass
    _write_meta(km, project, session)
    client = km.client()
    client.start_channels()
    client.wait_for_ready(timeout=warm_timeout)
    return km, client


def _write_meta(km, project: str, session: str) -> None:
    """Salva pid (e, su Windows, handle dell'evento di interrupt) del kernel:
    servono alle invocazioni successive della CLI, che non ne sono il padre."""
    proc = getattr(getattr(km, "provisioner", None), "process", None)
    if proc is None:
        return
    meta = {"pid": proc.pid, "started": time.time()}
    ev = getattr(proc, "win32_interrupt_event", None)
    if ev is not None:
        meta["interrupt_event"] = int(ev)
    try:
        _meta_file(project, session).write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        pass


def _kernel_process(project: str, session: str):
    """psutil.Process del kernel registrato nel sidecar, solo se e' davvero lui
    (la riga di comando cita il nostro connection file: i pid vengono riusati)."""
    pid = _read_meta(project, session).get("pid")
    if psutil is None or not pid:
        return None
    try:
        proc = psutil.Process(int(pid))
        cmdline = " ".join(proc.cmdline())
    except (psutil.Error, OSError, ValueError):
        return None
    cf_name = _connection_file(project, session).name
    return proc if cf_name.lower() in cmdline.lower() else None


def _windows_interrupt(project: str, session: str) -> bool:
    """Interrupt del kernel da un processo che NON lo ha lanciato.

    Su Windows ipykernel non gestisce `interrupt_request` (niente SIGINT): si
    interrompe solo segnalando l'evento Win32 ereditato dal kernel. Lo stesso
    valore di handle e' valido dentro il processo del kernel, quindi lo si
    duplica da li' nel processo corrente e lo si segnala.
    """
    meta = _read_meta(project, session)
    proc = _kernel_process(project, session)
    if proc is None or "interrupt_event" not in meta:
        return False
    try:
        import _winapi
        import ctypes
        PROCESS_DUP_HANDLE = 0x0040
        src = _winapi.OpenProcess(PROCESS_DUP_HANDLE, False, proc.pid)
        try:
            dup = _winapi.DuplicateHandle(
                src, int(meta["interrupt_event"]), _winapi.GetCurrentProcess(),
                0, False, _winapi.DUPLICATE_SAME_ACCESS,
            )
        finally:
            _winapi.CloseHandle(src)
        try:
            return bool(ctypes.windll.kernel32.SetEvent(ctypes.c_void_p(int(dup))))
        finally:
            _winapi.CloseHandle(int(dup))
    except Exception:  # noqa: BLE001
        return False


def interrupt_or_shutdown(km, project: str, session: str) -> None:
    """Interrompe la cella in corso; se non e' possibile spegne il kernel.
    Mai segnali POSIX verso un kernel di cui non siamo il padre."""
    try:
        if km is not None and km.has_kernel:
            km.interrupt_kernel()
            return
    except Exception:  # noqa: BLE001
        pass
    if IS_WINDOWS and _windows_interrupt(project, session):
        return
    shutdown_kernel(project, session)


def _kill_tree(proc) -> None:
    """Termina il kernel e i suoi discendenti con psutil (mai segnali POSIX).

    Su Windows il `python.exe` di un venv e' un launcher che avvia il vero
    interprete come processo figlio: il pid registrato e' quello del launcher,
    e uccidere il padre non uccide i figli. Per questo si scende nell'albero.
    """
    if proc is None or psutil is None:
        return
    try:
        if not proc.is_running():
            return
        victims = proc.children(recursive=True) + [proc]
    except psutil.Error:
        return
    for v in victims:
        with contextlib.suppress(psutil.Error):
            v.terminate()
    _, alive = psutil.wait_procs(victims, timeout=3.0)
    for v in alive:
        with contextlib.suppress(psutil.Error):
            v.kill()


def shutdown_kernel(project: str, session: str) -> None:
    cf = _connection_file(project, session)
    if not cf.exists():
        return
    from jupyter_client import KernelManager
    # Un KernelManager ricostruito dal connection file non possiede il processo:
    # `shutdown_kernel(now=True)` da solo non ucciderebbe nulla e lascerebbe un
    # kernel orfano. Sequenza: shutdown_request sul canale di controllo, attesa
    # dell'uscita, poi psutil terminate/kill. Nessun segnale POSIX.
    proc = _kernel_process(project, session)
    _use_selector_loop()
    try:
        km = KernelManager(connection_file=str(cf))
        km.load_connection_file()
        client = km.client()
        client.start_channels()
        try:
            client.shutdown()
            if proc is not None:
                try:
                    proc.wait(timeout=5.0)
                except psutil.Error:
                    pass
            else:
                time.sleep(1.0)
        finally:
            client.stop_channels()
        km.cleanup_resources(restart=False)
    except Exception:  # noqa: BLE001
        pass
    _kill_tree(proc)
    cf.unlink(missing_ok=True)
    _meta_file(project, session).unlink(missing_ok=True)


def _run_warm(client, notebook: Path, timeout: float = 60.0) -> None:
    """Execute the project's `<dir>/.notebook/warm.py` in the kernel namespace."""
    warm_py = notebook.parent / ".notebook" / "warm.py"
    if not warm_py.exists():
        return
    code = warm_py.read_text(encoding="utf-8")
    print("[notebook] cold-start: warming kernel + pre-importing heavy deps "
          "(may take 1-4 min for Qiskit/Torch)…", file=sys.stderr, flush=True)
    t0 = time.monotonic()
    try:
        client.execute_interactive(code, timeout=timeout, store_history=False, allow_stdin=False)
    except Exception as e:  # noqa: BLE001
        print(f"[notebook] warm.py partial failure: {e} (continuing)", file=sys.stderr)
    print(f"[notebook] warm done in {time.monotonic() - t0:.1f}s", file=sys.stderr, flush=True)


def _resolve_one(nb: dict, piece: str, *, full_selector: str) -> list[int]:
    """Resolve a SINGLE selector piece (tag/slice/id-list/bare-id) to indices.

    UX-2: shared helper for both single-mode and mixed-mode dispatch in
    `_resolve_cells`. `full_selector` is the original user-supplied string,
    quoted in error/warning messages so the operator sees what they typed.
    """
    cells = nb["cells"]
    sel = piece.strip()
    if not sel:
        return []
    if sel.startswith("tag:"):
        tag = sel[4:].strip()
        out = [i for i, c in enumerate(cells)
               if tag in c.get("metadata", {}).get("tags", [])]
        if not out:
            print(f"[notebook] WARNING: --cells '{full_selector}' piece "
                  f"'{piece}' matched 0 cells. Tag missing from plan? "
                  "(`tags: [...]` is required for tag selectors.)",
                  file=sys.stderr)
        return out
    if sel.startswith("id:"):
        # UX-2: explicit single-ID piece for mixed selectors. Strip `id:` and
        # treat the remainder as a bare ID; the bare-ID validator below will
        # reject `:` so `id:5:10` errors cleanly.
        sel = sel[3:].strip()
    if ":" in sel:
        lo, _, hi = sel.partition(":")
        try:
            lo_i = int(lo) if lo else 0
            hi_i = int(hi) if hi else len(cells)
        except ValueError:
            sys.exit(f"[notebook] selector piece {piece!r} (in {full_selector!r}) "
                     "is not a valid slice (expected `start:end`), "
                     "tag (`tag:NAME`), or ID (`id:NAME` or bare).")
        return list(range(max(0, lo_i), min(len(cells), hi_i)))
    ids = [s.strip() for s in sel.split(",") if s.strip()]
    bad = [i for i in ids if not all(ch.isalnum() or ch in "-_" for ch in i)]
    if bad:
        sys.exit(f"[notebook] selector piece {piece!r} (in {full_selector!r}) "
                 f"contains invalid ID chars: {bad!r}. "
                 "IDs must be alphanumeric / `-` / `_`.")
    out = [i for i, c in enumerate(cells) if c.get("id") in ids]
    found_ids = {cells[i].get("id") for i in out}
    missing = set(ids) - found_ids
    if missing:
        print(f"[notebook] WARNING: cell IDs not found: {sorted(missing)}",
              file=sys.stderr)
    return out


def _resolve_cells(nb: dict, selector: str | None) -> list[int]:
    """Explicit selector grammar with clear errors.

    UX-2: accepts MIXED selectors via top-level comma split when the selector
    contains heterogeneous prefixes (`tag:` and/or `id:` markers alongside
    other pieces). Each piece is resolved independently and the union of
    indices is returned (sorted, deduped). Backward compat: bare comma-IDs,
    single tag, and slice forms route through the single-piece fast path.
    """
    cells = nb["cells"]
    if selector is None:
        return list(range(len(cells)))
    sel = selector.strip()
    # UX-2: detect mixed-selector form. Trigger when there's a top-level comma
    # AND at least one piece carries a `tag:` or `id:` prefix. Pure bare-ID
    # comma lists, single tags, and slices keep the original single-mode path.
    pieces = [p.strip() for p in sel.split(",") if p.strip()]
    is_mixed = len(pieces) > 1 and any(
        p.startswith("tag:") or p.startswith("id:") for p in pieces
    )
    if is_mixed:
        union: set[int] = set()
        for piece in pieces:
            union.update(_resolve_one(nb, piece, full_selector=selector))
        return sorted(union)
    return _resolve_one(nb, sel, full_selector=selector)


def cmd_execute(args) -> int:
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        sys.exit(f"[notebook] not found: {nb_path}")
    project = _project_name(nb_path)
    session = _session_id(_project_root(nb_path))
    kernel_name = resolve_kernel_name(nb_path, getattr(args, "kernel_name", None))
    km, client = get_or_start_kernel(
        project, session, kernel_name=kernel_name, warm_timeout=args.warm_timeout,
    )
    if not args.no_warm:
        _run_warm(client, nb_path, timeout=args.warm_timeout)
    with notebook_lock(nb_path, timeout=args.lock_timeout):
        nb = nbformat.read(str(nb_path), as_version=4)
        targets = _resolve_cells(nb, args.cells)
        if not targets:
            print(f"[notebook] no cells matched selector {args.cells!r}; nothing to do.",
                  file=sys.stderr)
            return 0
        for idx in targets:
            cell = nb.cells[idx]
            if cell.cell_type != "code":
                continue
            try:
                _execute_cell(client, cell, idx,
                              timeout=args.cell_timeout,
                              heartbeat=args.iopub_heartbeat_timeout)
            except (TimeoutError, RuntimeError) as e:
                print(f"[notebook] cell {idx} failed: {e}; interrupting kernel.",
                      file=sys.stderr)
                interrupt_or_shutdown(km, project, session)
                raise
        atomic_write_ipynb(nb_path, nb)
    print(f"[notebook] executed {len(targets)} cell(s) on {nb_path}")
    return 0


def _execute_cell(client, cell, idx: int, *, timeout: float,
                  heartbeat: float = _HEARTBEAT_TIMEOUT) -> None:
    """V2-C1: single-threaded iopub drain + post-idle shell drain.

    All ZMQ I/O happens on the main thread (no thread-safety violation). A
    heartbeat watchdog detects dead kernels in <`heartbeat` seconds instead of
    after the full `cell_timeout`. Shell channel is drained AFTER iopub returns
    idle, on the same thread.

    V3-X3: `heartbeat` is configurable via `nb execute --iopub-heartbeat-timeout`
    so long Aer / Monte Carlo simulations that emit no intermediate iopub
    messages aren't killed by the watchdog. Default preserves V2-C1's
    fast-fail-on-dead-kernel intent for typical workloads.
    """
    msg_id = client.execute(cell.source, store_history=True, allow_stdin=False)
    outputs: list[dict] = []
    deadline = time.monotonic() + timeout
    last_msg_at = time.monotonic()

    # Drain iopub until execution_state=idle for our msg_id.
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise TimeoutError(f"cell {idx} exceeded {timeout}s")
        # V2-C1 watchdog: silent iopub for >heartbeat → liveness probe.
        if now - last_msg_at > heartbeat:
            if not _kernel_alive(client, timeout=2.0):
                raise RuntimeError(
                    f"cell {idx}: kernel appears dead "
                    f"(no iopub heartbeat for {now - last_msg_at:.1f}s; "
                    "kernel_info ZMQ probe timed out)"
                )
            last_msg_at = now  # reset; kernel is alive but slow
        try:
            msg = client.get_iopub_msg(timeout=min(remaining, 1.0))
        except Exception:  # noqa: BLE001
            continue
        last_msg_at = time.monotonic()
        if msg["parent_header"].get("msg_id") != msg_id:
            continue
        msg_type = msg["msg_type"]
        if msg_type == "status" and msg["content"]["execution_state"] == "idle":
            break
        if msg_type == "stream":
            outputs.append({
                "output_type": "stream",
                "name": msg["content"]["name"],
                "text": msg["content"]["text"],
            })
        elif msg_type in ("display_data", "execute_result"):
            entry = {
                "output_type": msg_type,
                "data": msg["content"].get("data", {}),
                "metadata": msg["content"].get("metadata", {}),
            }
            if msg_type == "execute_result":
                entry["execution_count"] = msg["content"].get("execution_count")
            outputs.append(entry)
        elif msg_type == "error":
            outputs.append({
                "output_type": "error",
                "ename": msg["content"]["ename"],
                "evalue": msg["content"]["evalue"],
                "traceback": msg["content"]["traceback"],
            })

    cell.outputs = outputs

    # Drain shell on the main thread. The execute_reply may already be queued
    # (commonly arrives before iopub idle on Linux) or may arrive shortly
    # after. A short timeout suffices.
    try:
        shell_deadline = time.monotonic() + 5.0
        while time.monotonic() < shell_deadline:
            try:
                reply = client.get_shell_msg(timeout=0.5)
            except Exception:  # noqa: BLE001
                continue
            if reply.get("parent_header", {}).get("msg_id") == msg_id \
                    and reply.get("msg_type") == "execute_reply":
                cell.execution_count = reply["content"].get("execution_count")
                break
    except Exception:  # noqa: BLE001
        pass


def cmd_warm(args) -> int:
    nb_path: Path = args.notebook.resolve()
    project = _project_name(nb_path)
    session = _session_id(_project_root(nb_path))
    kernel_name = resolve_kernel_name(nb_path, getattr(args, "kernel_name", None))
    km, client = get_or_start_kernel(
        project, session, kernel_name=kernel_name, warm_timeout=args.warm_timeout,
    )
    _run_warm(client, nb_path, timeout=args.warm_timeout)
    return 0


def cmd_reset_kernel(args) -> int:
    """V1-L3: requires notebook arg so project name resolves correctly."""
    if not args.notebook:
        sys.exit("[notebook] reset-kernel requires a notebook path: "
                 "`nb reset-kernel <path-to-ipynb>`.")
    nb_path: Path = args.notebook.resolve()
    project = _project_name(nb_path)
    session = _session_id(_project_root(nb_path))
    shutdown_kernel(project, session)
    print(f"[notebook] kernel reset: {project}-{session}")
    return 0
