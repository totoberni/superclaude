"""Test di fumo di scripts/job.py (start, wait, status, stop, list, supervisor morto).

Nessun test tocca il vero ~/.claude/tmp/jobs: JOB_ROOT punta a una cartella
temporanea ereditata dai sottoprocessi. I comandi dei job sono piccoli script
Python con sleep brevi; la fixture ferma ogni job rimasto e verifica che nessun
processo (supervisor o comando) sia sopravvissuto.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import job as jobmod  # noqa: E402

JOB_PY = SCRIPTS / "job.py"


# Su Windows ogni processo lanciato direttamente dal test riceve una console nascosta:
# senza il flag, fuori da una console condivisa aprirebbe una finestra visibile
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def cli(*args, timeout=60):
    """Esegue job.py come sottoprocesso (JOB_ROOT ereditato dall'ambiente del test)."""
    return subprocess.run(
        [sys.executable, str(JOB_PY), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=timeout, creationflags=NO_WINDOW,
    )


def start(name, code, *extra):
    """Avvia un job che esegue `python -c code`; extra = opzioni di start prima del `--`."""
    return cli("start", "--name", name, *extra, "--", sys.executable, "-c", code)


def status_of(root, name):
    return json.loads((root / name / "status.json").read_text(encoding="utf-8"))


def wait_until(predicate, seconds=10.0):
    """Attesa limitata nel test (mai nello strumento shell) di una condizione osservabile."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def jobs_root(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    monkeypatch.setenv("JOB_ROOT", str(root))
    yield root
    survivors = []
    for entry in (sorted(root.iterdir()) if root.is_dir() else []):
        if (entry / "status.json").is_file():
            cli("stop", entry.name)
            status = status_of(root, entry.name)
            for pid_key, created_key in (("pid", "pid_created"), ("child_pid", "child_created")):
                if jobmod._alive(status.get(pid_key), status.get(created_key)):
                    survivors.append((entry.name, pid_key, status.get(pid_key)))
    assert not survivors, f"processi rimasti vivi dopo il test: {survivors}"


def test_start_returns_fast_and_does_not_hold_pipes(jobs_root):
    # capture_output resterebbe appeso se il supervisor ereditasse le pipe del chiamante
    began = time.monotonic()
    result = start("fast", "import time; time.sleep(30)")
    elapsed = time.monotonic() - began
    assert result.returncode == 0, result.stderr
    assert elapsed < 10
    for key in ("id: fast", "pid: ", "log: ", "status: "):
        assert key in result.stdout
    status = status_of(jobs_root, "fast")
    assert status["state"] == "running"
    assert status["pid"] and status["exit_code"] is None
    for key in ("id", "pid", "cmd", "cwd", "started", "ended", "state", "exit_code", "marker", "log"):
        assert key in status
    # un id ancora in esecuzione viene rifiutato
    again = start("fast", "print(1)")
    assert again.returncode == 1 and "still running" in again.stderr


def test_wait_exit_zero(jobs_root):
    assert start("ok", "print('hello from job')").returncode == 0
    result = cli("wait", "ok", "--max", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "exit_code=0" in result.stdout and "hello from job" in result.stdout
    status = status_of(jobs_root, "ok")
    assert status["state"] == "exited" and status["exit_code"] == 0 and status["ended"]


def test_wait_nonzero_exit_returns_1(jobs_root):
    assert start("bad", "import sys; print('boom'); sys.exit(3)").returncode == 0
    result = cli("wait", "bad", "--max", "30")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "exit_code=3" in result.stdout
    assert status_of(jobs_root, "bad")["exit_code"] == 3


def test_wait_marker_returns_0_before_exit(jobs_root):
    assert start("mk", "import time; print('step', flush=True); print('ALL DONE', flush=True); time.sleep(30)",
                 "--marker", r"ALL\s+DONE").returncode == 0
    began = time.monotonic()
    result = cli("wait", "mk", "--max", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    assert time.monotonic() - began < 20
    assert "MARKER" in result.stdout
    assert status_of(jobs_root, "mk")["state"] == "running"  # il processo vive ancora


def test_wait_marker_option_overrides_and_exit_wins_when_already_finished(jobs_root):
    assert start("late", "import sys; print('READY', flush=True); sys.exit(5)").returncode == 0
    # Processo gia' terminato con codice 5: il codice di uscita prevale sul marker
    result = cli("wait", "late", "--max", "30", "--marker", "READY")
    assert result.returncode == 1 and "marker also seen" in result.stdout


def test_wait_stall_returns_3(jobs_root):
    assert start("stall", "import time; print('one line', flush=True); time.sleep(30)").returncode == 0
    began = time.monotonic()
    result = cli("wait", "stall", "--stall", "2", "--max", "30")
    assert result.returncode == 3, result.stdout + result.stderr
    assert time.monotonic() - began < 20
    assert "STALLED" in result.stdout


def test_wait_max_returns_2_while_output_keeps_flowing(jobs_root):
    code = "import time\nfor i in range(300):\n    print('tick', i, flush=True)\n    time.sleep(0.2)\n"
    assert start("busy", code).returncode == 0
    began = time.monotonic()
    result = cli("wait", "busy", "--max", "2", "--stall", "100")
    assert result.returncode == 2, result.stdout + result.stderr
    assert 1.5 < time.monotonic() - began < 20
    assert "tick" in result.stdout


def test_stop_kills_whole_process_tree(jobs_root, tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    grandchild = "import time; time.sleep(60)"
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    assert start("tree", code).returncode == 0
    assert wait_until(lambda: pid_file.is_file() and pid_file.read_text().strip())
    grandchild_pid = int(pid_file.read_text())
    grandchild_created = jobmod._proc_created(grandchild_pid)
    assert grandchild_created is not None  # il nipote e' vivo prima dello stop
    status = status_of(jobs_root, "tree")

    result = cli("stop", "tree")
    assert result.returncode == 0, result.stdout + result.stderr
    assert wait_until(lambda: not jobmod._alive(grandchild_pid, grandchild_created), 10)
    assert not jobmod._alive(status["child_pid"], status["child_created"])
    assert not jobmod._alive(status["pid"], status["pid_created"])
    assert status_of(jobs_root, "tree")["state"] == "stopped"
    assert cli("wait", "tree").returncode == 1  # stopped conta come non riuscito


def test_status_never_blocks_and_unknown_id_is_usage_error(jobs_root):
    assert start("st", "import time; print('line A', flush=True); time.sleep(30)").returncode == 0
    began = time.monotonic()
    result = cli("status", "st")
    assert time.monotonic() - began < 10
    assert result.returncode == 0
    assert '"state": "running"' in result.stdout and "line A" in result.stdout
    missing = cli("status", "does-not-exist")
    assert missing.returncode == 4 and "unknown job" in missing.stderr
    # `wait` su un id sconosciuto non deve mai somigliare a "ancora in esecuzione" (2)
    assert cli("wait", "does-not-exist").returncode == 4


def test_dead_supervisor_is_reported_as_crashed(jobs_root):
    assert start("dead", "import time; print('alive', flush=True); time.sleep(60)").returncode == 0
    status = status_of(jobs_root, "dead")
    assert jobmod._alive(status["child_pid"], status["child_created"])
    jobmod._kill(status["pid"], tree=False)  # uccide solo il supervisor, senza scrivere lo stato finale
    assert wait_until(lambda: not jobmod._alive(status["pid"], status["pid_created"]))
    if os.name == "nt":
        # Job Object KILL_ON_JOB_CLOSE: morto il supervisor, muore anche l'albero del comando
        assert wait_until(lambda: not jobmod._alive(status["child_pid"], status["child_created"]), 10)
    assert status_of(jobs_root, "dead")["state"] == "running"  # il file non lo sa ancora

    result = cli("status", "dead")
    assert '"state": "crashed"' in result.stdout
    waited = cli("wait", "dead", "--max", "30")
    assert waited.returncode == 1 and "CRASHED" in waited.stdout
    # stop ripulisce l'eventuale comando orfano (la fixture verifica che non resti nulla)
    assert cli("stop", "dead").returncode == 0


def test_command_not_found_fails_fast_without_creating_a_job(jobs_root):
    result = cli("start", "--name", "ghost", "--", "definitely_not_a_real_command_xyz")
    assert result.returncode == 1 and "not found" in result.stderr
    assert not (jobs_root / "ghost").exists()


def test_failure_to_spawn_is_recorded_as_crashed(jobs_root):
    # Percorso assoluto inesistente: supera il controllo PATH ma Popen fallisce nel supervisor
    missing = str(jobs_root / "no" / "such.exe")
    result = cli("start", "--name", "nospawn", "--", missing)
    assert result.returncode == 1
    assert status_of(jobs_root, "nospawn")["state"] == "crashed"
    assert "cannot start command" in (jobs_root / "nospawn" / "out.log").read_text(encoding="utf-8")


def test_list_shows_states(jobs_root):
    assert start("l-done", "print('x')").returncode == 0
    assert cli("wait", "l-done", "--max", "30").returncode == 0
    assert start("l-run", "import time; time.sleep(30)").returncode == 0
    rows = cli("list").stdout.splitlines()
    assert rows[0].split()[:3] == ["ID", "STATE", "AGE"]
    by_id = {line.split()[0]: line.split() for line in rows[1:]}
    assert by_id["l-done"][1] == "exited" and by_id["l-done"][3] == "0"
    assert by_id["l-run"][1] == "running"


def test_status_json_survives_concurrent_replace(jobs_root):
    # Windows: os.replace e lettura concorrente possono dare PermissionError; i retry devono assorbirli
    (jobs_root / "race").mkdir(parents=True)
    jobmod.write_status("race", {"n": 0})
    errors = []

    def writer():
        try:
            for i in range(300):
                jobmod.write_status("race", {"n": i})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(300):
            assert "n" in jobmod.read_status("race")
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    thread.join()
    assert not errors, errors


# --------------------------------------------------------------------------- revisione WH-7a


def test_wait_no_marker_and_empty_marker_ignore_the_start_marker(jobs_root):
    assert start("sticky", "import time; print('READY', flush=True); time.sleep(30)",
                 "--marker", "READY").returncode == 0
    first = cli("wait", "sticky", "--max", "30")
    assert first.returncode == 0 and "MARKER" in first.stdout
    # Il marker di start non e' appiccicoso: --no-marker e --marker "" aspettano exit/stallo/--max
    for extra in (["--no-marker"], ["--marker", ""]):
        again = cli("wait", "sticky", "--max", "2", "--stall", "100", *extra)
        assert again.returncode == 2 and "MARKER" not in again.stdout, extra
    both = cli("wait", "sticky", "--no-marker", "--marker", "X")
    assert both.returncode == 4  # opzioni mutuamente esclusive


def test_start_refuses_when_recorded_command_is_still_alive(jobs_root):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=NO_WINDOW)
    try:
        created = wait_until(lambda: jobmod._proc_created(sleeper.pid))
        assert created
        (jobs_root / "orphan").mkdir(parents=True)
        jobmod.write_status("orphan", {"id": "orphan", "state": "crashed", "pid": None, "cmd": ["x"],
                                       "child_pid": sleeper.pid, "child_created": created})
        result = start("orphan", "print(1)")
        assert result.returncode == 4
        assert "still alive" in result.stderr and "job.py stop orphan" in result.stderr
    finally:
        jobmod._kill(sleeper.pid, tree=True)
        sleeper.wait(timeout=10)


def test_marker_scanner_honours_deadline_byte_cap_and_chunk_boundaries(tmp_path, monkeypatch):
    chunk = 1024
    monkeypatch.setattr(jobmod, "SCAN_CHUNK_BYTES", chunk)
    monkeypatch.setattr(jobmod, "SCAN_MAX_BYTES_PER_POLL", 4 * chunk)
    log = tmp_path / "out.log"
    # SPLIT chiude il primo blocco e MARK apre il secondo: il marker attraversa il confine
    log.write_text("x" * (chunk - 5) + "SPLIT" + "MARK\n" + "filler line\n" * 2000, encoding="utf-8")
    pattern = re.compile("SPLITMARK")
    scanner = jobmod._MarkerScanner(log, pattern)
    assert scanner.scan(time.monotonic() + 60) is True and scanner.match == "SPLITMARK"

    late = tmp_path / "late.log"
    late.write_text("filler line\n" * 2000 + "THE END MARKER\n", encoding="utf-8")
    expired = jobmod._MarkerScanner(late, re.compile("THE END MARKER"))
    assert expired.scan(time.monotonic() - 1) is False
    assert expired.offset == chunk  # scadenza gia' passata: un solo blocco, poi si ferma
    capped = jobmod._MarkerScanner(late, re.compile("THE END MARKER"))
    assert capped.scan(time.monotonic() + 60) is False
    assert capped.offset == 4 * chunk  # tetto di byte per giro
    for _ in range(20):  # i giri successivi arrivano comunque al marker
        if capped.scan(time.monotonic() + 60):
            break
    assert capped.match == "THE END MARKER"


def test_handshake_timeout_kills_the_supervisor_before_reporting_crashed(jobs_root, monkeypatch):
    (jobs_root / "hs").mkdir(parents=True)
    jobmod.write_status("hs", {"id": "hs", "state": "running", "pid": None, "child_pid": None})
    fake_supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                       creationflags=NO_WINDOW)
    monkeypatch.setattr(jobmod, "HANDSHAKE_SECONDS", 0.5)
    try:
        status = jobmod._await_handshake("hs", fake_supervisor)
        assert status["state"] == "crashed"
        assert wait_until(lambda: fake_supervisor.poll() is not None, 10)
    finally:
        jobmod._kill(fake_supervisor.pid, tree=True)


def test_write_status_retries_transient_errors_and_gives_up_within_budget(jobs_root, monkeypatch):
    (jobs_root / "w").mkdir(parents=True)
    real_replace, calls = os.replace, {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(jobmod.os, "replace", flaky)
    jobmod.write_status("w", {"state": "x"})
    assert calls["n"] == 4 and jobmod.read_status("w") == {"state": "x"}

    def always_denied(src, dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(jobmod.os, "replace", always_denied)
    began = time.monotonic()
    with pytest.raises(PermissionError):
        jobmod.write_status("w", {"state": "y"}, budget=0.3)
    assert 0.3 <= time.monotonic() - began < 5
    assert not list((jobs_root / "w").glob("*.tmp"))


def test_supervisor_records_exit_even_if_the_pid_write_fails(jobs_root, tmp_path, monkeypatch):
    (jobs_root / "rc").mkdir(parents=True)
    status = {"id": "rc", "state": "running", "pid": None, "child_pid": None,
              "cmd": [sys.executable, "-c", "print('ran')"], "cwd": str(tmp_path)}
    jobmod.write_status("rc", status)
    real_update = jobmod._update_status

    def flaky(job_id, _budget=jobmod.IO_BUDGET_SECONDS, **changes):
        if "child_pid" in changes:
            raise PermissionError(13, "Access is denied")
        return real_update(job_id, _budget, **changes)

    monkeypatch.setattr(jobmod, "_update_status", flaky)
    assert jobmod._run_command("rc", status) == 0
    final = status_of(jobs_root, "rc")
    assert final["state"] == "exited" and final["exit_code"] == 0
    log = (jobs_root / "rc" / "out.log").read_text(encoding="utf-8")
    assert "ran" in log and "could not record pids" in log


def test_supervisor_leaves_the_exit_code_in_the_log_if_the_final_write_fails(jobs_root, tmp_path, monkeypatch):
    (jobs_root / "fw").mkdir(parents=True)
    status = {"id": "fw", "state": "running", "pid": None, "child_pid": None,
              "cmd": [sys.executable, "-c", "import sys; sys.exit(7)"], "cwd": str(tmp_path)}
    jobmod.write_status("fw", status)
    real_update = jobmod._update_status

    def flaky(job_id, _budget=jobmod.IO_BUDGET_SECONDS, **changes):
        if changes.get("state") == "exited":
            raise PermissionError(13, "Access is denied")
        return real_update(job_id, _budget, **changes)

    monkeypatch.setattr(jobmod, "_update_status", flaky)
    jobmod._run_command("fw", status)
    assert "could not record exit code 7" in (jobs_root / "fw" / "out.log").read_text(encoding="utf-8")
    # Il "supervisor" qui e' pytest stesso: senza chiudere lo stato la fixture farebbe `stop` su di lui
    real_update("fw", state="exited", exit_code=7, pid=None)


def run_main(capsys, *args):
    """Esegue job.main in-process (validazione di argomenti e stato, senza lanciare processi)."""
    try:
        code = jobmod.main(list(args))
    except SystemExit as exc:  # il parser esce con EXIT_USAGE
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_finished_job(root, name):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "out.log").write_text("done\n", encoding="utf-8")
    jobmod.write_status(name, {"id": name, "state": "exited", "exit_code": 0, "pid": None, "cmd": ["x"]})


@pytest.mark.parametrize("content, message", [
    (b"{not json", "corrupt"),
    (b"[1, 2]", "not a JSON object"),
    (b"\xff\xfe\x00", "corrupt"),
    (b'{"id": "x"}', "no valid 'state'"),
])
def test_corrupt_status_is_a_usage_error_without_traceback(jobs_root, capsys, content, message):
    (jobs_root / "broken").mkdir(parents=True)
    (jobs_root / "broken" / "status.json").write_bytes(content)
    try:
        for command in (("wait", "broken"), ("status", "broken"), ("stop", "broken")):
            code, _, err = run_main(capsys, *command)
            assert code == 4, (command, err)
            assert message in err and "Traceback" not in err
        code, out, err = run_main(capsys, "list")
        assert code == 0 and "broken" in out and not err
    finally:
        shutil.rmtree(jobs_root / "broken")  # la fixture non sa leggere uno stato corrotto


@pytest.mark.parametrize("bad_id", [
    "../x", "a/b", "a\\b", "C:x", "..", ".", "", "nul", "NUL", "Con", "aux.log", "COM1", "lpt9.txt", "a.",
    "x" * 65, "has space",
])
def test_invalid_job_ids_are_rejected_before_touching_the_disk(jobs_root, tmp_path, capsys, bad_id):
    for command in (("status", bad_id), ("start", "--name", bad_id, "--", sys.executable, "-c", "print(1)")):
        code, _, err = run_main(capsys, *command)
        assert code == 4 and "invalid job id" in err, (command, err)
    assert not (tmp_path / "x").exists() and not jobs_root.exists()


@pytest.mark.parametrize("good_id", ["com10", "nul-job", "a.b", "Job_1", "x" * 64])
def test_ordinary_job_ids_are_accepted(good_id):
    assert jobmod._check_id(good_id) == good_id


@pytest.mark.parametrize("option, value", [
    ("--max", "nan"), ("--max", "inf"), ("--max", "-1"), ("--max", "abc"), ("--stall", "nan"), ("--stall", "inf"),
])
def test_wait_rejects_non_finite_or_negative_limits(jobs_root, capsys, option, value):
    write_finished_job(jobs_root, "lim")
    code, _, err = run_main(capsys, "wait", "lim", option, value)
    assert code == 4 and "Traceback" not in err


def test_wait_clamps_max_below_the_tool_call_cap(jobs_root, capsys):
    write_finished_job(jobs_root, "clamp")
    code, out, err = run_main(capsys, "wait", "clamp", "--max", "99999")
    assert code == 0 and "EXITED exit_code=0" in out, out + err
    assert "clamped to 580s" in err
