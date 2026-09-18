"""/notebook skill — IO primitives.

Atomic write, advisory lock, JupyterLab race detector, lock-path resolver,
git canonical-repo-root resolution, SHA-256 helpers, git-clean check.

NEVER call `nbformat.write(nb, path)` directly — it is non-atomic per
upstream source. Always go through `atomic_write_ipynb`.

V1.1 — addresses example-project-review-of-skill-v1.md issues:
  - V1-H7: lock file lives at canonical-repo-root; `nb init` adds
           `*.ipynb.lock` to `.gitignore` to prevent accidental commits.
  - V1-H21: `assert_git_clean_or_force` adds optional working-tree check.
  - V1-M3: second `git rev-parse --is-bare-repository` call wrapped.
  - V1-L8: lock_path_for now docstrings the relative_to ValueError condition.
  - V1-L9: mtime threshold raised to 100 ms (FS-granularity-blind floor).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import psutil
except ImportError:
    psutil = None  # JL detector becomes mtime-only fallback

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


# ---------------------------------------------------------------------------
# Port Windows: helper di piattaforma (interprete del venv, percorsi, lock).
# ---------------------------------------------------------------------------


def venv_python(root: Path) -> Path | None:
    """Interprete del venv di progetto: `.venv/Scripts/python.exe` su Windows,
    `.venv/bin/python` su POSIX. Restituisce None se il venv non esiste."""
    root = Path(root)
    if IS_WINDOWS:
        cand = root / ".venv" / "Scripts" / "python.exe"
    else:
        cand = root / ".venv" / "bin" / "python"
    return cand if cand.is_file() else None


def find_project_python(start: Path) -> Path | None:
    """Risale da `start` verso la radice cercando un `.venv` di progetto."""
    cur = Path(os.path.abspath(str(start)))
    if not cur.is_dir():
        cur = cur.parent
    for d in (cur, *cur.parents):
        py = venv_python(d)
        if py is not None:
            return py
    return None


def fwd(path: Path | str) -> str:
    """Percorso con slash in avanti: l'unica forma sicura dentro i comandi
    eseguiti dallo `sh` di Git (merge driver, hook) e in `.git/config`."""
    return str(path).replace("\\", "/")


def same_path(a: Path | str, b: Path | str) -> bool:
    """Confronto di percorsi: su Windows ignora maiuscole e tipo di separatore."""
    return (os.path.normcase(os.path.normpath(str(a)))
            == os.path.normcase(os.path.normpath(str(b))))


def _is_under(path: str, parent: str) -> bool:
    # Si normalizza a mano (non con os.path) cosi' il confronto resta
    # insensibile a maiuscole e separatori anche nei test eseguiti su POSIX.
    p = path.replace("\\", "/").rstrip("/").lower()
    par = parent.replace("\\", "/").rstrip("/").lower()
    return bool(par) and (p == par or p.startswith(par + "/"))


def onedrive_root_for(path: Path | str) -> str | None:
    """Radice OneDrive che contiene `path`, oppure None. Le radici arrivano
    dalle variabili d'ambiente impostate dal client OneDrive."""
    candidates = [str(path)]
    try:
        candidates.append(str(Path(path).resolve()))
    except OSError:
        pass
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(var)
        if root and any(_is_under(c, root) for c in candidates):
            return root
    return None


def is_network_path(path: Path | str) -> bool:
    """True per i percorsi UNC (anche nella forma `\\\\?\\UNC\\`) e per le
    lettere di unita' mappate su una condivisione di rete."""
    s = str(path).replace("/", "\\")
    if s.upper().startswith("\\\\?\\UNC\\"):
        return True
    if s.startswith("\\\\"):
        # `\\?\C:\...` e `\\.\...` sono prefissi locali, non di rete
        return not s.startswith(("\\\\?\\", "\\\\.\\"))
    if IS_WINDOWS and len(s) >= 2 and s[1] == ":":
        try:
            import ctypes
            # 4 = DRIVE_REMOTE
            return ctypes.windll.kernel32.GetDriveTypeW(s[:2] + "\\") == 4
        except Exception:  # noqa: BLE001
            return False
    return False


def path_hazards(path: Path | str) -> list[str]:
    """Avvisi (mai rifiuti) per le posizioni problematiche su Windows."""
    out: list[str] = []
    od = onedrive_root_for(path)
    if od:
        out.append(
            f"{path} is inside a OneDrive-synced folder ({od}). The sync client "
            "can hold .ipynb/.py files open while uploading: expect sporadic "
            "'file in use' errors on atomic replace, spurious mtime-drift "
            "conflicts and conflict copies. Prefer a non-synced location "
            "(e.g. ~/code/<project>) or pause OneDrive sync while working."
        )
    if is_network_path(path):
        out.append(
            f"{path} is on a network/UNC path. Byte-range locks and atomic "
            "replace are unreliable over SMB and I/O is slow; prefer a local disk."
        )
    return out


def _try_lock_fd(fd: int) -> bool:
    """Lock esclusivo NON bloccante. Windows: `msvcrt.locking` su 1 byte
    all'offset 0. POSIX: `fcntl.flock`. In entrambi i casi il sistema operativo
    rilascia il lock alla chiusura dell'handle, quindi anche se il processo muore."""
    if IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            # EACCES / EDEADLOCK: byte gia' bloccato da un altro handle
            if e.errno in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", 36)):
                return False
            raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EACCES):
            return False
        raise


def _unlock_fd(fd: int) -> None:
    if IS_WINDOWS:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # la chiusura dell'handle rilascia comunque il lock
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def replace_with_retry(src: str, dst: str, *, attempts: int = 20,
                       delay: float = 0.1) -> None:
    """`os.replace` con breve retry: su Windows fallisce con PermissionError se
    un altro processo (antivirus, OneDrive, JupyterLab, indexer) tiene aperta
    la destinazione."""
    last: OSError | None = None
    for _ in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last = e
            if not IS_WINDOWS:
                break
            time.sleep(delay)
    raise PermissionError(
        f"could not replace {dst}: another process holds it open (editor, "
        f"JupyterLab, antivirus or OneDrive sync?). Close it and retry. "
        f"Original error: {last}"
    ) from last


def normalize_lf(path: Path) -> bool:
    """Riporta a LF un file scritto da jupytext. Su Windows jupytext apre i file
    in modalita' testo senza `newline`, quindi produce CRLF sia nel `.py` sia
    nell'`.ipynb`: i diff di git e il pairing ne uscirebbero sporchi.
    L'mtime viene conservato, perche' `jupytext --sync` sceglie la direzione in
    base agli mtime. Restituisce True se il file e' stato riscritto."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if b"\r\n" not in data:
        return False
    st = path.stat()
    tmp = path.with_name(f".{path.name}.lf.tmp")
    tmp.write_bytes(data.replace(b"\r\n", b"\n"))
    replace_with_retry(str(tmp), str(path))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return True


class NotebookConflict(RuntimeError):
    """Raised when an external writer mutated the notebook since our last read."""


class LockPathError(RuntimeError):
    """Raised when a notebook is outside the canonical repo root."""


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_repo_root(start: Path | None = None) -> Path:
    """Return the path that all worktrees of a repo agree on.

    Uses ``git rev-parse --path-format=absolute --git-common-dir`` and takes
    the parent. The ``--path-format=absolute`` flag is critical: without it,
    git returns a relative ``.git`` path from the main worktree, which breaks
    cross-worktree resolution. (See claude-code #36275, opencode #16995.)

    V1-M3: both subprocess calls are guarded.
    """
    cwd = str(start) if start else None
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"not in a git repo (cwd={cwd}): {e}") from e
    try:
        bare = subprocess.check_output(
            ["git", "rev-parse", "--is-bare-repository"], cwd=cwd, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"git --is-bare-repository failed (cwd={cwd}): {e}") from e
    if bare == "true":
        raise RuntimeError("notebook ops require a working tree (bare repo refused)")
    return Path(out).resolve().parent


def lock_path_for(notebook_path: Path) -> Path:
    """Sentinel lock path for a notebook, located under the canonical repo root.

    Per-notebook granularity: different notebooks in the same repo can be
    written in parallel.

    V3-X1: works correctly inside linked worktrees. The relative path is
    computed against the *worktree* root (so the notebook always sits under
    it), then anchored to the *canonical* repo root so two worktrees editing
    the same notebook produce the same lock file (shared-lock invariant).

    Raises:
        LockPathError: if `notebook_path` is outside its enclosing worktree
            (or fall-back canonical root) — should be unreachable in normal
            use; guards against pathological symlink layouts.

    Note: `nb init` adds `*.ipynb.lock` to `.gitignore` so this file is never
    accidentally committed (V1-H7).
    """
    nb_abs = notebook_path.resolve()
    canonical = canonical_repo_root(nb_abs.parent)
    try:
        worktree_root = Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(nb_abs.parent), text=True, stderr=subprocess.DEVNULL,
        ).strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fall back to canonical root (preserves pre-V3 behaviour outside
        # worktrees / when git rev-parse fails).
        worktree_root = canonical
    try:
        rel = nb_abs.relative_to(worktree_root)
    except ValueError as e:
        raise LockPathError(
            f"notebook {nb_abs} is outside its worktree root {worktree_root} "
            f"(canonical={canonical})"
        ) from e
    return canonical / f"{rel}.ipynb.lock"


@contextmanager
def notebook_lock(notebook_path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Exclusive non-blocking lock on a sidecar `.ipynb.lock`.

    `fcntl.flock` (POSIX) / `msvcrt.locking` (Windows) is auto-released by the
    OS when the handle closes, so it survives crashes of the holder.

    Formato del file: il byte 0 e' quello bloccato (su Windows il lock e'
    obbligatorio e ne impedisce la lettura agli altri processi); il testo
    `<pid> <epoch>` parte dall'offset 1.
    """
    lp = lock_path_for(notebook_path)
    with file_lock(lp, timeout=timeout):
        yield


@contextmanager
def file_lock(lp: Path, timeout: float = 30.0) -> Iterator[None]:
    """Lock esclusivo su un file sentinella qualsiasi (vedi `notebook_lock`)."""
    lp = Path(lp)
    lp.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = os.open(str(lp), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    locked = False
    try:
        while True:
            if _try_lock_fd(fd):
                locked = True
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, f"\n{os.getpid()} {time.time():.0f}\n".encode())
                break
            if time.monotonic() > deadline:
                holder = _read_lock_holder(lp)
                raise TimeoutError(
                    f"lock held by {holder or '<unknown>'}: {lp} "
                    f"(timeout {timeout}s; bump --lock-timeout to wait longer)"
                )
            time.sleep(0.1)
        yield
    finally:
        try:
            if locked:
                _unlock_fd(fd)
        finally:
            os.close(fd)


def _read_lock_holder(lp: Path) -> str | None:
    # Si legge dall'offset 1: il byte 0 e' quello bloccato dal detentore.
    try:
        fd = os.open(str(lp), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        return os.read(fd, 256).decode("utf-8", "replace").strip() or None
    except OSError:
        return None
    finally:
        os.close(fd)


def assert_no_jl_writer(notebook_path: Path, baseline_mtime: float | None = None,
                        *, mtime_eps: float = 0.1) -> None:
    """Detect JupyterLab/server holding the notebook + mtime drift.

    V1-L9: ``mtime_eps`` raised from 1 ms to 100 ms — universal floor that
    works on any FS (NTFS has ~10 ms granularity, ext4 ~1 ns).

    Hard-fails (raises) — orchs don't read prompts. Detector pair:
    - mtime drift: cheap, catches any external writer.
    - psutil scan: identifies JL specifically, even if it hasn't written yet.

    JL 4.x writes `.ipynb` directly with no `.ipynb_checkpoints/` shadow on save —
    `.ipynb_checkpoints/` mtime is unreliable in JL 4 (per JL #17365).
    """
    nb_abs = notebook_path.resolve()
    if baseline_mtime is not None:
        try:
            cur = nb_abs.stat().st_mtime
        except FileNotFoundError:
            return
        if abs(cur - baseline_mtime) > mtime_eps:
            raise NotebookConflict(
                f"mtime drifted: external writer touched {nb_abs} "
                f"(baseline={baseline_mtime}, current={cur})"
            )
    if psutil is None:
        return
    nb_str = str(nb_abs)
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "jupyter" not in name:
                continue
            for of in proc.open_files():
                if same_path(of.path, nb_str):
                    raise NotebookConflict(
                        f"jupyter process pid={proc.info['pid']} ({name}) has "
                        f"{nb_abs} open. Close it (or stop pid {proc.info['pid']}) and retry."
                    )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def assert_git_clean_or_force(notebook_path: Path, force: bool = False) -> None:
    """V1-H21: refuse to mutate when the working tree has uncommitted changes,
    unless `--force` is set. Targeted check on the notebook + paired .py only.

    V2-H1: filter out untracked (`?? `) entries — a brand-new `.ipynb` (e.g.,
    one freshly generated by `jupytext --sync`) is "untracked", not "dirty".
    The `clean tree` semantic only applies to TRACKED files with uncommitted
    modifications. Untracked-only paths slip through; modified-or-staged paths
    still raise.
    """
    if force:
        return
    nb_abs = notebook_path.resolve()
    py = nb_abs.with_suffix(".py")
    paths_to_check = [nb_abs] + ([py] if py.exists() else [])
    # V3-X1: gate on the *worktree's* index (what the user expects), not the
    # main worktree's. `git -C <worktree_root> status` checks that worktree.
    try:
        canonical_repo_root(nb_abs.parent)  # ensures we're in a non-bare repo
    except RuntimeError:
        return  # not in a git repo; skip the check
    try:
        worktree_root = Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(nb_abs.parent), text=True, stderr=subprocess.DEVNULL,
        ).strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return  # detached/odd state; skip rather than mis-report
    for p in paths_to_check:
        try:
            rel = str(p.relative_to(worktree_root))
        except ValueError:
            continue  # path outside this worktree — caller's problem, not ours
        try:
            status = subprocess.check_output(
                ["git", "-C", str(worktree_root), "status", "--porcelain", "--", rel],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            continue
        # V2-H1: drop untracked entries; only modified/staged tracked files
        # are "dirty" for our purposes.
        modified = [ln for ln in status.splitlines() if not ln.startswith("?? ")]
        if modified:
            raise NotebookConflict(
                f"working tree has uncommitted changes to {rel}: \n  "
                + "\n  ".join(modified) +
                f"\nEither commit/stash them, or pass --force to override."
            )


def atomic_write_ipynb(path: Path, nb: dict, *, indent: int = 1) -> None:
    """Atomic write of a notebook dict.

    Pattern: tempfile in same dir → flush → fsync → os.replace (+ dir-fsync on
    POSIX). On Windows `os.replace` is MoveFileEx(REPLACE_EXISTING); it is
    retried briefly when another process holds the target open.
    Always uses ``newline="\n"`` to suppress CRLF injection.

    DO NOT call ``nbformat.write(nb, path)`` directly — verified non-atomic
    in the upstream source (`nbformat/__init__.py`: plain `fp.write(s)`).
    """
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(nb, tmp, indent=indent, ensure_ascii=False)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        replace_with_retry(tmp.name, str(path))
        if not IS_WINDOWS:
            # Su Windows una directory non si apre con os.open: niente dir-fsync.
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


def load_ipynb(path: Path) -> dict:
    """Load a notebook as a plain dict (so callers don't need nbformat for read)."""
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def cmd_lock_status(args) -> int:
    """`nb lock-status <nb>` — print holder PID + start-time if any."""
    lp = lock_path_for(args.notebook)
    if not lp.exists():
        print(f"[notebook] no lock file at {lp}")
        return 0
    holder = _read_lock_holder(lp)
    if not holder:
        print(f"[notebook] lock file empty at {lp} (orphan?)")
        return 0
    print(f"[notebook] lock file: {lp}")
    print(f"[notebook] holder: {holder}")
    pid_str = holder.split(maxsplit=1)[0] if holder else ""
    if psutil is not None and pid_str.isdigit():
        pid = int(pid_str)
        if psutil.pid_exists(pid):
            print(f"[notebook] PID {pid} alive — lock is current.")
        else:
            print(f"[notebook] PID {pid} DEAD: stale holder text (the OS already released "
                  f"the lock); safe to delete: {lp}")
    return 0
