#!/usr/bin/env python3
"""job.py - lancia comandi lunghi in background e li attende in chiamate limitate.

Uso (da shell: `scpy job.py <sottocomando> ...`):
  start --name ID [--marker REGEX] [--cwd DIR] -- CMD...   lancia staccato, torna subito
  status ID                                                 JSON + ultime 20 righe, mai bloccante
  wait ID [--max 540] [--stall 300] [--marker REGEX | --no-marker]
                                                            attende exit, marker, stallo o --max
  stop ID                                                   termina l'intero albero di processi
  list                                                      tutti i job con stato, eta', exit code

Codici di uscita di `wait`:
  0 = uscito con 0, oppure marker visto mentre il processo gira
  1 = uscito con codice diverso da 0 (anche stopped/crashed, oppure uscito non zero dopo il marker)
  2 = ancora in esecuzione a --max
  3 = stallo (out.log fermo per --stall secondi mentre il processo vive)
  4 = errore d'uso o job sconosciuto (mai confondibile con "ancora in esecuzione")

Stato in <data home>/tmp/jobs/<id>/: out.log (stdout+stderr) e status.json.
La variabile d'ambiente JOB_ROOT sostituisce la cartella radice `jobs` (usata dai test).
In status.json `pid` e' il pid del SUPERVISOR (anchor dell'albero), `child_pid` quello del comando.

Il marker e' un'espressione regolare fidata (scritta da chi lancia il job), applicata riga per
riga: un'espressione patologica puo' comunque bloccare il confronto di una singola riga.
`wait` usa il marker di `start` salvo `--marker REGEX` (anche vuoto) o `--no-marker`. `--max` e'
limitato a 580 s per stare sotto il tetto di 600 s della tool call.
Su Windows il supervisor e il suo albero stanno in un Job Object KILL_ON_JOB_CLOSE: se il
supervisor muore, muore tutto cio' che il comando ha avviato (e a fine job i residui vengono chiusi).

Il comando e' in forma exec (argv, nessuna shell). Per pipeline o redirect usare
esplicitamente `-- bash -c "..."`. Sui comandi Python viene impostato PYTHONUNBUFFERED=1
(se non gia' definito): senza, l'output a blocchi farebbe sembrare il job in stallo.

Interfaccia condivisa: plans/worker-hygiene/plan.md, sezione "Shared interface".
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

EXIT_OK, EXIT_FAILED, EXIT_RUNNING, EXIT_STALLED, EXIT_USAGE = 0, 1, 2, 3, 4

POLL_SECONDS = 1.0
TAIL_LINES = 20
TAIL_BYTES = 64 * 1024
# Il controllo della scadenza avviene tra un blocco e l'altro: blocchi piccoli, byte per giro limitati
SCAN_CHUNK_BYTES = 256 * 1024
SCAN_MAX_BYTES_PER_POLL = 16 * 1024 * 1024
CARRY_MAX_CHARS = 64 * 1024
DEFAULT_MAX_SECONDS = 540
DEFAULT_STALL_SECONDS = 300
MAX_WAIT_CAP_SECONDS = 580
HANDSHAKE_SECONDS = 10.0
# Un job con pid ancora vuoto e' "in avvio": oltre questo limite il supervisor non e' mai partito
STARTING_GRACE_SECONDS = 30.0
KILL_WAIT_SECONDS = 5.0
# Budget di tempo per i retry su PermissionError (Windows, file aperto da un altro processo)
IO_BUDGET_SECONDS = 10.0
FINAL_WRITE_BUDGET_SECONDS = 20.0
IO_BACKOFF_START = 0.005
IO_BACKOFF_MAX = 0.1
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Nomi di dispositivo riservati da Windows, anche con estensione (aux.log) e in qualunque maiuscola
WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
)

# Flag di creazione processo Windows (valori Win32 documentati)
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
STILL_ACTIVE = 259
EPOCH_DELTA_SECONDS = 11644473600  # 1601-01-01 -> 1970-01-01


class JobError(Exception):
    """Errore atteso: messaggio su stderr e codice di uscita dedicato."""

    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- percorsi

def jobs_root() -> Path:
    override = os.environ.get("JOB_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # sc_profile vive in <code home>/lib: il percorso si ricava da questo file
    lib_dir = str(Path(__file__).resolve().parents[1] / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import sc_profile

    return sc_profile.data_home() / "tmp" / "jobs"


def _job_dir(job_id: str) -> Path:
    return jobs_root() / job_id


def _log_path(job_id: str) -> Path:
    return _job_dir(job_id) / "out.log"


def _status_path(job_id: str) -> Path:
    return _job_dir(job_id) / "status.json"


def _check_id(job_id: str) -> str:
    # L'id diventa il nome di una cartella: niente separatori, "..", punto finale o nomi riservati Windows
    valid = (ID_PATTERN.match(job_id) and not job_id.endswith(".")
             and job_id.split(".")[0].upper() not in WINDOWS_RESERVED_STEMS)
    if not valid:
        raise JobError(f"invalid job id {job_id!r}: letters, digits, '.', '_', '-' (max 64); "
                       "no trailing dot, no Windows reserved names (CON, NUL, COM1, ...)", EXIT_USAGE)
    return job_id


# --------------------------------------------------------------------------- tempo

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _age_seconds(iso_text: str | None) -> float:
    if not iso_text:
        return 0.0
    try:
        return max(0.0, (datetime.now().astimezone() - datetime.fromisoformat(iso_text)).total_seconds())
    except ValueError:
        return 0.0


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60}m"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# --------------------------------------------------------------------------- status.json

def _retry_io(action, budget: float = IO_BUDGET_SECONDS):
    """Esegue `action` ripetendo su PermissionError con backoff esponenziale entro un budget di tempo.

    Windows: os.replace e la lettura si ostacolano a vicenda (e antivirus o indexer tengono il file
    aperto un istante). Il backoff con jitter evita che lettore e scrittore restino in fase.
    """
    deadline = time.monotonic() + budget
    delay = IO_BACKOFF_START
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay * random.uniform(0.5, 1.5))
            delay = min(delay * 2, IO_BACKOFF_MAX)


def read_status(job_id: str) -> dict | None:
    path = _status_path(job_id)
    try:
        data = _retry_io(lambda: json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except ValueError as exc:  # JSONDecodeError e UnicodeDecodeError
        raise JobError(f"status of job {job_id!r} is corrupt ({exc}); remove {path.parent} and start again",
                       EXIT_USAGE) from exc
    if not isinstance(data, dict):
        raise JobError(f"status of job {job_id!r} is not a JSON object; remove {path.parent} and start again",
                       EXIT_USAGE)
    return data


def write_status(job_id: str, status: dict, budget: float = IO_BUDGET_SECONDS) -> None:
    """Scrittura atomica: file temporaneo nella stessa cartella + os.replace."""
    path = _status_path(job_id)
    tmp = path.with_name(f"status.{os.getpid()}.tmp")

    def publish() -> None:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(status, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)

    try:
        _retry_io(publish, budget)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _update_status(job_id: str, _budget: float = IO_BUDGET_SECONDS, **changes) -> dict:
    status = read_status(job_id) or {}
    status.update(changes)
    write_status(job_id, status, _budget)
    return status


# --------------------------------------------------------------------------- processi

@functools.lru_cache(maxsize=1)
def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    return k32


def _windows_created(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return round(ticks / 1e7 - EPOCH_DELTA_SECONDS, 3)
    finally:
        k32.CloseHandle(handle)


# Il handle resta aperto per tutta la vita del supervisor: chiuderlo (anche per morte) uccide l'albero
_JOB_HANDLE = None


def _enable_kill_on_close() -> str | None:
    """Mette il processo corrente in un Job Object KILL_ON_JOB_CLOSE (solo Windows).

    I processi avviati dopo ereditano l'appartenenza al job, quindi non c'e' finestra di corsa.
    Ritorna il testo dell'errore, oppure None se ok (o se non applicabile).
    """
    global _JOB_HANDLE
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = _kernel32()
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        return f"CreateJobObject failed (winerror {ctypes.get_last_error()})"
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                       ctypes.byref(limits), ctypes.sizeof(limits)):
        error = f"SetInformationJobObject failed (winerror {ctypes.get_last_error()})"
    elif not k32.AssignProcessToJobObject(handle, k32.GetCurrentProcess()):
        error = f"AssignProcessToJobObject failed (winerror {ctypes.get_last_error()})"
    else:
        _JOB_HANDLE = handle
        return None
    k32.CloseHandle(handle)
    return error


def _posix_created(pid: int) -> float | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        import psutil

        return round(psutil.Process(pid).create_time(), 3)
    except Exception:
        return 0.0  # vivo ma istante di creazione ignoto: il controllo di identita' viene saltato


def _proc_created(pid: int | None) -> float | None:
    """Istante di creazione del processo se e' vivo, altrimenti None."""
    if not pid:
        return None
    return _windows_created(pid) if os.name == "nt" else _posix_created(pid)


def _alive(pid: int | None, created: float | None = None) -> bool:
    """Vivo e, se noto l'istante di creazione, davvero lo stesso processo (i pid Windows si riciclano)."""
    current = _proc_created(pid)
    if current is None:
        return False
    return not (created and current and abs(current - created) > 2.0)


def _kill(pid: int, tree: bool) -> None:
    if os.name == "nt":
        command = ["taskkill", "/F", "/PID", str(pid)] + (["/T"] if tree else [])
        subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False,
                       creationflags=CREATE_NO_WINDOW)
        return
    import signal

    try:
        # Il comando gira in una propria sessione: pgid == pid
        (os.killpg if tree else os.kill)(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_and_wait(pid: int | None, created: float | None, tree: bool) -> bool:
    """Uccide solo se il pid e' ancora il nostro processo. True se alla fine non e' piu' vivo."""
    if not pid or not _alive(pid, created):
        return True
    _kill(pid, tree)
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _alive(pid, created):
            return True
        time.sleep(0.05)
    return not _alive(pid, created)


# --------------------------------------------------------------------------- caricamento job

def load_job(job_id: str) -> dict | None:
    """Legge lo stato e rileva un supervisor morto con stato ancora 'running' (-> crashed)."""
    status = read_status(job_id)
    if status is None or status.get("state") != "running":
        return status
    if status.get("pid") is None:
        if _age_seconds(status.get("started")) < STARTING_GRACE_SECONDS:
            return status
    elif _alive(status["pid"], status.get("pid_created")):
        return status
    # Rilettura: la scrittura finale del supervisor precede la sua morte
    status = read_status(job_id) or status
    if status.get("state") != "running":
        return status
    status.update(state="crashed", ended=_now_iso())
    write_status(job_id, status)
    return status


def _require_job(job_id: str) -> dict:
    _check_id(job_id)
    status = load_job(job_id)
    if status is None:
        raise JobError(f"unknown job {job_id!r}", EXIT_USAGE)
    if not isinstance(status.get("state"), str):
        raise JobError(f"status of job {job_id!r} has no valid 'state'; remove {_job_dir(job_id)} and start again",
                       EXIT_USAGE)
    return status


def _tail(path: Path, count: int = TAIL_LINES) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # la prima riga e' probabilmente troncata
    return lines[-count:]


def _print_tail(job_id: str) -> None:
    print(f"--- out.log (last {TAIL_LINES} lines) ---")
    for line in _tail(_log_path(job_id)):
        print(line)


# --------------------------------------------------------------------------- supervisor

def _resolve_argv(argv: list[str]) -> list[str] | None:
    """Risolve il nome nudo via PATH/PATHEXT (npm -> npm.cmd). None se non trovato."""
    first = argv[0]
    if os.sep in first or (os.altsep and os.altsep in first):
        return argv
    found = shutil.which(first)
    return [found, *argv[1:]] if found else None


def _command_flags() -> dict:
    if os.name == "nt":
        # Il supervisor non ha console: senza CREATE_NO_WINDOW ogni figlio console aprirebbe una finestra
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _log_note(log, text: str) -> None:
    log.write(f"[job.py] {text}\n".encode("utf-8"))


def _record(job_id: str, log, what: str, budget: float = IO_BUDGET_SECONDS, **changes) -> bool:
    """Scrive lo stato senza mai interrompere il supervisor: se fallisce lascia una riga in out.log."""
    try:
        _update_status(job_id, budget, **changes)
        return True
    except (OSError, JobError) as exc:
        _log_note(log, f"could not record {what}: {exc}")
        return False


def _run_command(job_id: str, status: dict) -> int:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    argv = _resolve_argv(status["cmd"]) or status["cmd"]
    with open(_log_path(job_id), "ab", buffering=0) as log:
        try:
            proc = subprocess.Popen(argv, cwd=status["cwd"], stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, env=env, close_fds=True, **_command_flags())
        except OSError as exc:
            _log_note(log, f"cannot start command {argv!r}: {exc}")
            _update_status(job_id, state="crashed", ended=_now_iso())
            return EXIT_FAILED
        # Un errore di scrittura qui non deve saltare proc.wait(): il comando e' gia' partito
        _record(job_id, log, "pids", pid=os.getpid(), pid_created=_proc_created(os.getpid()),
                child_pid=proc.pid, child_created=_proc_created(proc.pid))
        code = proc.wait()
        # Lo stato finale e' l'unica traccia dell'exit code: budget lungo, e in caso di fallimento
        # il codice resta almeno in out.log
        _record(job_id, log, f"exit code {code}", FINAL_WRITE_BUDGET_SECONDS,
                state="exited", exit_code=code, ended=_now_iso())
    return EXIT_OK


def cmd_supervise(job_id: str) -> int:
    """Processo detached: esegue il comando, ne attende la fine e scrive lo stato finale."""
    status = read_status(job_id)
    if status is None:
        return EXIT_FAILED
    try:
        warning = _enable_kill_on_close()
        if warning:
            with open(_log_path(job_id), "ab") as log:
                _log_note(log, f"warning: command tree is not bound to the supervisor: {warning}")
        return _run_command(job_id, status)
    except Exception:
        with open(_log_path(job_id), "ab") as log:
            _log_note(log, f"supervisor error:\n{traceback.format_exc()}")
            _record(job_id, log, "crash", state="crashed", ended=_now_iso())
        return EXIT_FAILED


# --------------------------------------------------------------------------- start

def _supervisor_python() -> str:
    """Interprete del supervisor: su Windows pythonw.exe (sottosistema GUI, nessuna console).

    Con DETACHED_PROCESS il flag CREATE_NO_WINDOW viene ignorato: lo stub python.exe del venv,
    senza console, avvia l'interprete base che ne riceve una nuova e visibile.
    """
    if os.name == "nt":
        windowed = Path(sys.executable).with_name("pythonw.exe")
        if windowed.is_file():
            return str(windowed)
    return sys.executable


def _spawn_supervisor(job_id: str) -> subprocess.Popen:
    # Sotto pythonw sys.stdout e sys.stderr sono None: il supervisor non deve stampare
    argv = [_supervisor_python(), str(Path(__file__).resolve()), "_supervise", job_id]
    # JOB_ROOT esplicito: supervisor e chiamante devono concordare sulla cartella radice
    env = {**os.environ, "JOB_ROOT": str(jobs_root())}
    # stdin/stdout/stderr su DEVNULL + close_fds: nessun handle della shell chiamante
    # viene ereditato, altrimenti la tool call resterebbe appesa finche' il job vive
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _abort_supervisor(job_id: str, supervisor: subprocess.Popen) -> None:
    """Handshake fallito: ferma supervisor e comando prima di dichiarare crashed (niente scritture tardive)."""
    if supervisor.poll() is None:
        _kill(supervisor.pid, tree=True)  # lo stub del venv e l'interprete reale sotto di lui
    status = read_status(job_id) or {}
    _kill_and_wait(status.get("pid"), status.get("pid_created"), tree=True)
    _kill_and_wait(status.get("child_pid"), status.get("child_created"), tree=True)


def _await_handshake(job_id: str, supervisor: subprocess.Popen) -> dict:
    """Attende che il supervisor abbia avviato il comando (child_pid) o fallito."""
    deadline = time.monotonic() + HANDSHAKE_SECONDS
    while True:
        status = read_status(job_id) or {}
        if status.get("child_pid") or status.get("state") != "running":
            return status
        if supervisor.poll() is not None or time.monotonic() >= deadline:
            _abort_supervisor(job_id, supervisor)
            return _update_status(job_id, state="crashed", ended=_now_iso())
        time.sleep(0.05)


def _compile_marker(marker: str | None) -> re.Pattern | None:
    if not marker:
        return None
    try:
        return re.compile(marker, re.MULTILINE)
    except re.error as exc:
        raise JobError(f"invalid marker regex {marker!r}: {exc}", EXIT_USAGE) from exc


def _refuse_if_busy(job_id: str, existing: dict) -> None:
    if existing.get("state") == "running":
        raise JobError(f"job {job_id!r} is still running (pid {existing.get('pid')}); stop it first")
    # Qualunque sia lo stato, un comando ancora vivo andrebbe perso di vista da un nuovo start
    if _alive(existing.get("child_pid"), existing.get("child_created")):
        raise JobError(f"job {job_id!r} is {existing.get('state')} but its command (pid {existing.get('child_pid')}) "
                       f"is still alive; run `job.py stop {job_id}` first", EXIT_USAGE)


def cmd_start(args: argparse.Namespace, command: list[str]) -> int:
    job_id = _check_id(args.name)
    if not command:
        raise JobError("start needs a command after '--'", EXIT_USAGE)
    _compile_marker(args.marker)
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()
    if not cwd.is_dir():
        raise JobError(f"--cwd is not a directory: {cwd}", EXIT_USAGE)
    if _resolve_argv(command) is None:
        raise JobError(f"command not found on PATH: {command[0]}")
    existing = load_job(job_id)
    if existing:
        _refuse_if_busy(job_id, existing)
    _job_dir(job_id).mkdir(parents=True, exist_ok=True)
    try:
        _log_path(job_id).write_bytes(b"")
    except PermissionError as exc:
        raise JobError(f"cannot reset {_log_path(job_id)}: held open by another process") from exc
    write_status(job_id, {
        "id": job_id, "pid": None, "cmd": command, "cwd": str(cwd), "started": _now_iso(), "ended": None,
        "state": "running", "exit_code": None, "marker": args.marker, "log": str(_log_path(job_id)),
        "pid_created": None, "child_pid": None, "child_created": None,
    })
    status = _await_handshake(job_id, _spawn_supervisor(job_id))
    print(f"id: {job_id}")
    print(f"pid: {status.get('pid')}")
    print(f"child_pid: {status.get('child_pid')}")
    print(f"log: {_log_path(job_id)}")
    print(f"status: {_status_path(job_id)}")
    if status.get("state") == "crashed":
        print("error: the command could not be started; see the log", file=sys.stderr)
        _print_tail(job_id)
        return EXIT_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------- status / list / stop

def cmd_status(args: argparse.Namespace) -> int:
    status = _require_job(args.id)
    print(json.dumps(status, indent=2))
    _print_tail(args.id)
    return EXIT_OK


def cmd_list(_args: argparse.Namespace) -> int:
    root = jobs_root()
    rows = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            status = load_job(entry.name)
        except (OSError, ValueError, JobError):
            status = {"state": "unreadable"}
        if status is None:
            continue
        exit_code = status.get("exit_code")
        rows.append((entry.name, status.get("state", "?"), _fmt_duration(_age_seconds(status.get("started"))),
                     "-" if exit_code is None else str(exit_code), str(status.get("pid") or "-"),
                     " ".join(status.get("cmd") or [])[:60]))
    if not rows:
        print("(no jobs)")
        return EXIT_OK
    header = ("ID", "STATE", "AGE", "EXIT", "PID", "CMD")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    status = _require_job(args.id)
    child_pid = status.get("child_pid")
    child_alive = bool(child_pid) and _alive(child_pid, status.get("child_created"))
    if status["state"] in ("exited", "stopped") or (status["state"] == "crashed" and not child_alive):
        print(f"job {args.id}: already {status['state']}")
        return EXIT_OK
    # Prima il supervisor (cosi' non puo' piu' scrivere uno stato 'exited'), poi l'albero del comando
    clean = _kill_and_wait(status.get("pid"), status.get("pid_created"), tree=False)
    clean = _kill_and_wait(child_pid, status.get("child_created"), tree=True) and clean
    if not clean:
        raise JobError(f"job {args.id}: a process survived the kill (pid {status.get('pid')} / {child_pid})")
    _update_status(args.id, state="stopped", ended=_now_iso())
    print(f"job {args.id}: STOPPED (killed supervisor pid {status.get('pid')}, command tree pid {child_pid})")
    return EXIT_OK


# --------------------------------------------------------------------------- wait

_LINE_BREAKS = re.compile(r"[\r\n]+")


class _MarkerScanner:
    """Cerca il marker in out.log riga per riga, leggendo solo i byte nuovi.

    L'ultima riga incompleta viene riportata al giro dopo. Il pattern e' fidato (vedi docstring
    del modulo): la scadenza e il tetto di byte per giro limitano il tempo speso su un log grande,
    non il costo di una singola riga con un'espressione patologica.
    """

    def __init__(self, path: Path, pattern: re.Pattern | None) -> None:
        self.path, self.pattern = path, pattern
        self.offset, self.carry, self.match = 0, "", None

    def scan(self, deadline: float) -> bool:
        """True se il marker e' stato visto. Dopo ogni blocco si ferma se `deadline` (monotonic) e'
        scaduta o se i byte del giro hanno raggiunto SCAN_MAX_BYTES_PER_POLL: il resto al giro dopo."""
        if self.pattern is None:
            return False
        if self.match is not None:
            return True
        scanned = 0
        try:
            if self.path.stat().st_size < self.offset:  # log troncato: si riparte
                self.offset, self.carry = 0, ""
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                while chunk := handle.read(SCAN_CHUNK_BYTES):
                    self.offset += len(chunk)
                    scanned += len(chunk)
                    if self._scan_text(self.carry + chunk.decode("utf-8", errors="replace")):
                        return True
                    if scanned >= SCAN_MAX_BYTES_PER_POLL or time.monotonic() >= deadline:
                        break
        except OSError:
            pass
        return False

    def _scan_text(self, text: str) -> bool:
        pieces = _LINE_BREAKS.split(text)
        for piece in pieces:
            found = self.pattern.search(piece) if piece else None
            if found:
                self.match = found.group(0)
                return True
        self.carry = pieces[-1][-CARRY_MAX_CHARS:]
        return False


def _log_signature(path: Path) -> tuple[int, float]:
    """(dimensione, istante dell'ultima modifica) di out.log; (0, adesso) se manca."""
    try:
        info = path.stat()
        return info.st_size, min(info.st_mtime, time.time())
    except OSError:
        return 0, time.time()


def _finished_headline(job: dict, marker_seen: bool) -> tuple[str, int]:
    state = job["state"]
    extra = " (marker also seen)" if marker_seen else ""
    if state == "exited":
        code = job.get("exit_code")
        return f"EXITED exit_code={code}{extra}", EXIT_OK if code == 0 else EXIT_FAILED
    if state == "crashed":
        return "CRASHED (supervisor died before recording an exit code)", EXIT_FAILED
    return state.upper(), EXIT_FAILED


def _report(job_id: str, headline: str, code: int, waited: float) -> int:
    print(f"job {job_id}: {headline}; waited {_fmt_duration(waited)}")
    _print_tail(job_id)
    return code


def _wait_marker(args: argparse.Namespace, job: dict) -> str | None:
    """--no-marker disattiva, --marker (anche vuoto) sostituisce, altrimenti vale quello di `start`."""
    if args.no_marker:
        return None
    return args.marker if args.marker is not None else job.get("marker")


def cmd_wait(args: argparse.Namespace) -> int:
    job = _require_job(args.id)
    if args.max > MAX_WAIT_CAP_SECONDS:
        print(f"job.py: --max {args.max:g} clamped to {MAX_WAIT_CAP_SECONDS}s (tool call cap is 600s)",
              file=sys.stderr)
        args.max = MAX_WAIT_CAP_SECONDS
    scanner = _MarkerScanner(_log_path(args.id), _compile_marker(_wait_marker(args, job)))
    began = time.monotonic()
    deadline = began + args.max
    last_size, last_change = _log_signature(_log_path(args.id))
    while True:
        job = load_job(args.id) or job
        marker_seen = scanner.scan(deadline)
        waited = time.monotonic() - began
        if job["state"] != "running":
            headline, code = _finished_headline(job, marker_seen)
            return _report(args.id, headline, code, waited)
        if marker_seen:
            return _report(args.id, f"MARKER matched {scanner.match!r} (process still running)", EXIT_OK, waited)
        size, _ = _log_signature(_log_path(args.id))
        if size != last_size:
            last_size, last_change = size, time.time()
        idle = time.time() - last_change
        if idle >= args.stall:
            return _report(args.id, f"STALLED no output for {_fmt_duration(idle)} (process still running)",
                           EXIT_STALLED, waited)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _report(args.id, f"RUNNING still running after --max {args.max:g}s", EXIT_RUNNING, waited)
        time.sleep(min(POLL_SECONDS, remaining))


# --------------------------------------------------------------------------- CLI

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        # Il codice 2 di argparse significherebbe "ancora in esecuzione" per `wait`
        self.print_usage(sys.stderr)
        print(f"job.py: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _finite_non_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be a finite number >= 0, got {text!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="job.py", description="Run long commands detached and wait for them in bounded calls.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start --name ID [--marker RE] [--cwd DIR] -- CMD...")
    start.add_argument("--name", required=True)
    start.add_argument("--marker")
    start.add_argument("--cwd")
    status = sub.add_parser("status", help="one-shot JSON + last log lines")
    status.add_argument("id")
    wait = sub.add_parser("wait", help="block until exit, marker, stall or --max")
    wait.add_argument("id")
    wait.add_argument("--max", type=_finite_non_negative, default=DEFAULT_MAX_SECONDS)
    wait.add_argument("--stall", type=_finite_non_negative, default=DEFAULT_STALL_SECONDS)
    marker_group = wait.add_mutually_exclusive_group()
    marker_group.add_argument("--marker", help="regex; overrides the marker given to start (empty = none)")
    marker_group.add_argument("--no-marker", action="store_true", help="ignore the marker given to start")
    stop = sub.add_parser("stop", help="kill the whole process tree")
    stop.add_argument("id")
    sub.add_parser("list", help="all jobs with state")
    supervise = sub.add_parser("_supervise")  # interno: lanciato da `start`
    supervise.add_argument("id")
    return parser


def _split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Per `start` il comando segue il primo `--`: va tolto prima di argparse (che lo tratta male)."""
    if argv and argv[0] == "start" and "--" in argv:
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1:]
    return argv, []


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    head, command = _split_command(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(head)
    handlers = {"status": cmd_status, "wait": cmd_wait, "stop": cmd_stop, "list": cmd_list}
    try:
        if args.command == "start":
            return cmd_start(args, command)
        if args.command == "_supervise":
            return cmd_supervise(args.id)
        return handlers[args.command](args)
    except JobError as exc:
        print(f"job.py: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
