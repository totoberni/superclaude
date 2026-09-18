"""Test dei port Windows: statusline, cost computer, recover_worker, spawn_log_summary.

Nessun test tocca il vero ~/.claude: i sottoprocessi girano con USERPROFILE/HOME
puntati a una directory temporanea, i test in-process sovrascrivono le costanti
di percorso dei moduli con monkeypatch.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "swarm"))

STATUSLINE = SCRIPTS / "statusline_telemetry.py"

SAMPLE_STDIN = {
    "session_id": "test-session",
    "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
    "effort": {"level": "high"},
    "context_window": {
        "used_percentage": 42,
        "context_window_size": 200000,
        "current_usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 80000,
        },
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": 1790000000},
        "seven_day": {"used_percentage": 30, "resets_at": 1790500000},
    },
}


def _fake_home_env(home):
    """Ambiente per sottoprocessi con la home rediretta (Windows e POSIX)."""
    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    env.pop("STATUSLINE_STDIN", None)
    return env


def _fresh_cost_cache(claude_dir):
    """Cache costi fresca: impedisce allo statusline di lanciare il refresh detached."""
    (claude_dir / ".cost-cache.json").write_text(
        json.dumps({"schema": 1, "day_usd": 1.5, "week_usd": 2.5, "month_usd": 3.5,
                    "total_usd": 4.5, "subscription_eur_total": 10.0,
                    "updated": time.time()}),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# statusline
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utf8_mode", ["1", "0"])
def test_statusline_end_to_end(tmp_path, utf8_mode):
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    _fresh_cost_cache(claude)
    payload = dict(SAMPLE_STDIN, cwd=str(tmp_path))  # tmp_path non e' un repo git

    env = _fake_home_env(home)
    env["COLUMNS"] = "140"
    env["PYTHONUTF8"] = utf8_mode
    if utf8_mode == "0":
        env["PYTHONIOENCODING"] = "cp1252"  # simula la console Windows legacy
    proc = subprocess.run(
        [sys.executable, str(STATUSLINE)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("utf-8")
    assert out.strip()
    assert "42%" in out and "Opus 4.8" in out and "$1.50" in out
    assert "\u2588" in out  # glifo della barra, emesso in UTF-8 in entrambi i modi

    ctx = json.loads((claude / ".context-latest.json").read_text(encoding="utf-8"))
    assert set(ctx) == {"schema", "ts", "used_pct", "window_size", "used_tokens"}
    assert ctx["schema"] == 1
    assert isinstance(ctx["ts"], int)
    assert ctx["used_pct"] == 42.0 and isinstance(ctx["used_pct"], float)
    assert ctx["window_size"] == 200000
    assert ctx["used_tokens"] == 83000
    # nessun file temporaneo residuo della scrittura atomica
    assert not list(claude.glob("*.tmp"))

    rate = json.loads((claude / ".rate-latest.json").read_text(encoding="utf-8"))
    assert rate["seven_day_pct"] == 30 and rate["five_hour_pct"] == 12


def test_statusline_empty_stdin_never_blank(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    _fresh_cost_cache(home / ".claude")
    proc = subprocess.run(
        [sys.executable, str(STATUSLINE)], input=b"not json",
        capture_output=True, env=_fake_home_env(home), timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip()
    assert not (home / ".claude" / ".context-latest.json").exists()


@pytest.fixture
def sl(monkeypatch):
    import statusline_telemetry as mod
    return mod


def test_terminal_width_fallback(sl, monkeypatch):
    monkeypatch.setattr(sl, "_width_from_tty", lambda: None)
    monkeypatch.setenv("COLUMNS", "123")
    assert sl.terminal_width({}) == 123
    # il campo di stdin ha precedenza su COLUMNS
    assert sl.terminal_width({"terminal_width": 90}) == 90
    # valori assurdi (pixel) scartati
    monkeypatch.setenv("COLUMNS", "99999")
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(0, 0): os.terminal_size((0, 0)))
    assert sl.terminal_width({}) == sl.WIDTH_UNKNOWN_DEFAULT
    monkeypatch.delenv("COLUMNS")
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(0, 0): os.terminal_size((111, 40)))
    assert sl.terminal_width({}) == 111


def test_width_from_tty_never_raises(sl):
    w = sl._width_from_tty()
    assert w is None or sl.TERM_COLS_MIN <= w <= sl.TERM_COLS_MAX


def test_git_tolerates_missing_repo_and_dir(sl, tmp_path):
    ctx = {"stdin": {"cwd": str(tmp_path)}, "cols": 140}
    seg = sl.seg_dirgit(ctx)
    assert seg and tmp_path.name in seg
    ctx = {"stdin": {"cwd": str(tmp_path / "does-not-exist")}, "cols": 140}
    assert "does-not-exist" in sl.seg_dirgit(ctx)
    assert sl._git_parallel(str(tmp_path), [["rev-parse", "HEAD"]]) == [None]


def test_cost_refresh_is_detached_python_and_throttled(sl, tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))

    monkeypatch.setattr(sl.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sl, "COST_REFRESH_STAMP", str(tmp_path / ".stamp"))
    sl._fire_cost_refresh()
    sl._fire_cost_refresh()  # frenato dal timbro: nessun secondo spawn
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("cost_cache_computer.py") and os.path.isfile(cmd[1])
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    if os.name == "nt":
        flags = kw["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert flags & subprocess.CREATE_NO_WINDOW
        assert "start_new_session" not in kw
    else:
        assert kw["start_new_session"] is True


def test_replace_retry_gives_up_on_locked_target(sl, tmp_path, monkeypatch):
    n = []

    def boom(a, b):
        n.append(1)
        raise PermissionError("locked")

    monkeypatch.setattr(sl.os, "replace", boom)
    with pytest.raises(PermissionError):
        sl._replace_retry("a", "b", attempts=3, delay=0)
    assert len(n) == 3


# --------------------------------------------------------------------------- #
# cost_cache_computer
# --------------------------------------------------------------------------- #
def _assistant(msg_id, req_id, model, usage, ts):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "requestId": req_id,
        "message": {"id": msg_id, "model": model, "usage": usage},
    })


@pytest.fixture
def cost(tmp_path, monkeypatch):
    import cost_cache_computer as mod
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    monkeypatch.setattr(mod, "PROJECTS_DIR", str(claude / "projects"))
    monkeypatch.setattr(mod, "CACHE_PATH", str(claude / ".cost-cache.json"))
    monkeypatch.setattr(mod, "LEDGER_PATH", str(claude / ".cost-ledger.json"))
    monkeypatch.setattr(mod, "RATE_LATEST_PATH", str(claude / ".rate-latest.json"))
    return mod, claude


def test_cost_computer_synthetic_tree(cost):
    mod, claude = cost
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # nome di progetto derivato da un percorso Windows + sottocartella subagents
    proj = claude / "projects" / "C--Users-mario-rossi-code-demo"
    sub = proj / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    lines = [
        _assistant("m1", "r1", "claude-opus-4-8", usage, now),
        _assistant("m1", "r1", "claude-opus-4-8", usage, now),      # duplicato
        json.dumps({"type": "user", "message": {"content": "usage"}}),
        '{"type": "assistant", "message": {"usage": TRUNCATED',    # riga corrotta
    ]
    (proj / "sess-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (sub / "agent-abc.jsonl").write_text(
        _assistant("m2", "r2", "claude-sonnet-4-6",
                   {"input_tokens": 0, "output_tokens": 1_000_000}, now) + "\n"
        + _assistant("m3", "r3", "<synthetic>", usage, now) + "\n",
        encoding="utf-8",
    )
    (proj / "bad-bytes.jsonl").write_bytes(b'\xff\xfe "usage" garbage\n')

    assert mod.main() == 0
    cache = json.loads((claude / ".cost-cache.json").read_text(encoding="utf-8"))
    # opus 4.8: 1M input = $5; sonnet 4.6: 1M output = $15; duplicato e synthetic esclusi
    assert cache["total_usd"] == pytest.approx(20.0)
    assert cache["trailing_1d_usd"] == pytest.approx(20.0)
    assert cache["unpriced_models"] == {}
    assert cache["schema"] == 1 and cache["updated"] > 0
    ledger = json.loads((claude / ".cost-ledger.json").read_text(encoding="utf-8"))
    assert sum(ledger["days"].values()) == pytest.approx(20.0)
    assert not list(claude.glob("*.tmp"))


def test_cost_computer_locked_cache_gives_up_silently(cost, monkeypatch):
    mod, claude = cost
    (claude / "projects" / "p").mkdir()
    monkeypatch.setattr(mod, "replace_retry", lambda tmp, dst, **kw: False)
    # ledger occupato -> percorso degradato; cache occupata -> rinuncia, rc 0
    assert mod.main() == 0
    assert not (claude / ".cost-cache.json").exists()
    assert not list(claude.glob("*.tmp"))


def test_cost_long_path_prefix(cost):
    mod, _ = cost
    p = mod._long_path(os.getcwd())
    if os.name == "nt":
        assert p.startswith("\\\\?\\")
        assert mod._long_path(p) == p
    else:
        assert p == os.getcwd()


# --------------------------------------------------------------------------- #
# recover_worker
# --------------------------------------------------------------------------- #
def _run_recover(*args):
    env = dict(os.environ, PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "swarm" / "recover_worker.py"), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )


def test_recover_agent_mode(tmp_path):
    f = tmp_path / "agent-x.jsonl"
    rows = [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "first"},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "C:/w/b.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/w/a.py"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/w/b.py"}},
            {"type": "text", "text": "final words \u00e8 " + "x" * 2500},
        ]}},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + '\n{"type": "assist',
                 encoding="utf-8")
    proc = _run_recover("agent", str(f))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith("## Agent transcript recovery: ")
    assert "final words \u00e8" in out and "first" not in out
    assert "[... truncated at 2000 of" in out
    assert "### tool_use event count\n4\n" in out
    assert "C:/w/a.py\nC:/w/b.py" in out
    assert len(out) < 3200


def test_recover_agent_no_text_and_no_paths(tmp_path):
    f = tmp_path / "agent-y.jsonl"
    f.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "q"}}]}}) + "\n",
        encoding="utf-8")
    out = _run_recover("agent", str(f)).stdout
    assert "### tool_use event count\n1\n" in out
    assert out.rstrip().endswith("(none)")


def test_recover_task_mode(tmp_path):
    f = tmp_path / "t1.output"
    f.write_text('{"x": 1}\n' + json.dumps(
        {"message": {"content": [{"type": "text", "text": "task result"}]}}) + "\n",
        encoding="utf-8")
    proc = _run_recover("task", str(f))
    assert proc.returncode == 0
    assert proc.stdout == "## Task output recovery: %s\n\ntask result\n" % f

    f.write_text(json.dumps({"message": {"content": "bare string"}}) + "\n", encoding="utf-8")
    assert "bare string" in _run_recover("task", str(f)).stdout

    f.write_text("plain log line\nlast raw line\n\n", encoding="utf-8")
    assert _run_recover("task", str(f)).stdout.rstrip().endswith("last raw line")


def test_recover_errors(tmp_path):
    proc = _run_recover("task", str(tmp_path / "missing.output"))
    assert proc.returncode == 1 and "not found" in proc.stderr
    proc = _run_recover("bogus")
    assert proc.returncode == 1 and "usage:" in proc.stderr
    empty = tmp_path / "empty.output"
    empty.write_text("", encoding="utf-8")
    assert _run_recover("task", str(empty)).returncode == 1


# --------------------------------------------------------------------------- #
# spawn_log_summary
# --------------------------------------------------------------------------- #
@pytest.fixture
def spawn_log(tmp_path):
    rows = [
        "2026-05-08T10:00:00Z\tmeta\tw-explorer\tlook around",
        "2026-05-09T11:00:00Z\tmeta\tw-implementer\tdo it",
        "2026-05-09T16:30:00Z\tmain\tw-explorer\tlook again",
        "2026-05-10T09:00:00Z\tmeta\tw-explorer\tthird",
    ]
    p = tmp_path / "_spawns.log"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p, rows


def test_spawn_log_summary_modes(spawn_log, capsys):
    import spawn_log_summary as sls
    path, rows = spawn_log

    assert sls.main([], log_path=str(path)) == 0
    out = capsys.readouterr().out
    assert "## Spawn Log Summary" in out and "Total events: 4" in out
    assert "      3 w-explorer\n      1 w-implementer" in out
    assert "      3 meta\n      1 main" in out
    assert out.rstrip().endswith(rows[-1])

    sls.main(["--by-type", "2026-05-09"], log_path=str(path))
    assert capsys.readouterr().out == "      2 w-explorer\n      1 w-implementer\n"

    sls.main(["--by-parent", "2026-05-09T16"], log_path=str(path))
    assert capsys.readouterr().out == "      1 main\n      1 meta\n"

    sls.main(["--recent"], log_path=str(path))
    assert capsys.readouterr().out.splitlines() == rows

    sls.main(["2026-05-10"], log_path=str(path))
    assert "Total events: 1" in capsys.readouterr().out


def test_spawn_log_summary_missing_log(tmp_path, capsys):
    import spawn_log_summary as sls
    assert sls.main([], log_path=str(tmp_path / "nope.log")) == 0
    assert capsys.readouterr().out.strip() == "No spawn log found"


# --------------------------------------------------------------------------- #
# utilita' del gruppo C
# --------------------------------------------------------------------------- #
def test_deps_pip_install_targets_venv_python(monkeypatch, capsys):
    import better_super_deps as bsd
    manifest = SCRIPTS.parent / "dependencies.yml"
    data = bsd.load_manifest(manifest)
    assert "docker" not in data
    names = [e["name"] for e in data["python"]]
    assert "markdown" in names and "pytest" in names and "aiogram" not in names

    fake_py = Path(sys.executable)
    monkeypatch.setattr(bsd, "VENV_PYTHON", fake_py)

    class Args:
        dry_run = True

    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "UV" if name == "uv" else None)
    with pytest.raises(SystemExit) as exc:
        bsd.cmd_pip_install(Args(), data)
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("UV pip install --python %s " % fake_py)

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        bsd.cmd_pip_install(Args(), data)
    assert capsys.readouterr().out.startswith("%s -m pip install " % fake_py)


def test_deps_record_honours_manifest(tmp_path):
    # regressione: --record scriveva sempre ~/.claude/dependencies.yml ignorando --manifest
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    manifest = tmp_path / "custom-deps.yml"
    manifest.write_text('schema: 1\nupdated: "2000-01-01"\n\npython: []\n\nnpm: []\n', encoding="utf-8")

    def record(version):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "better_super_deps.py"), "--manifest", str(manifest),
             "--record", "--type", "python", "--name", "demo-pkg", "--version", version,
             "--via", "test", "--reason", "regression"],
            env=_fake_home_env(home), capture_output=True, text=True, encoding="utf-8",
        )

    res = record("1.0")
    assert res.returncode == 0, res.stderr
    assert "Recorded python/demo-pkg" in res.stdout
    res = record("1.1")                       # idempotente per (type, name): aggiorna, non duplica
    assert res.returncode == 0 and "Updated python/demo-pkg" in res.stdout

    import better_super_deps as bsd
    entries = bsd.load_manifest(manifest)["python"]
    assert [(e["name"], str(e["version"])) for e in entries] == [("demo-pkg", "1.1")]
    # il manifest predefinito (sotto la home finta) non deve essere stato creato
    assert not (home / ".claude" / "dependencies.yml").exists()


def test_deps_venv_python_path_is_platform_correct():
    import better_super_deps as bsd
    tail = bsd.VENV_PYTHON.parts[-2:]
    assert tail == (("Scripts", "python.exe") if os.name == "nt" else ("bin", "python"))


def _load_ssot():
    spec = importlib.util.spec_from_file_location("ssot_lint", SCRIPTS / "ssot-lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(os.name != "nt", reason="ricerca di sh specifica di Windows")
def test_ssot_lint_finds_git_bash_sh(tmp_path, monkeypatch):
    mod = _load_ssot()
    bindir = tmp_path / "Git" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "bash.exe").write_bytes(b"")
    (bindir / "sh.exe").write_bytes(b"")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bindir / "bash.exe"))
    assert mod.find_sh() == str(bindir / "sh.exe")


@pytest.mark.skipif(os.name != "nt", reason="ricerca di sh specifica di Windows")
def test_ssot_lint_skips_ground_truth_without_sh(monkeypatch, capsys):
    mod = _load_ssot()
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    failures = []
    mod.check_ground_truth({"id": "c1", "ground_truth": "false"}, failures)
    mod.check_ground_truth({"id": "c2", "ground_truth": "false"}, failures)
    assert failures == []
    err = capsys.readouterr().err
    assert err.count("SKIP ground_truth") == 1 and "CLAUDE_CODE_GIT_BASH_PATH" in err


def test_plan_index_uses_current_interpreter():
    sys.path.insert(0, str(SCRIPTS / "plan"))
    import plan_index
    assert plan_index.MEMORY_DB == Path.home() / ".claude" / "scripts" / "memory" / "memory_db.py"
    assert not hasattr(plan_index, "VENV_PYTHON")
