"""Test delle correzioni pre-attivazione del nucleo degli hook (A2, A4, A7, A8).

A2: Monitor e' uno strumento di shell per ogni guardia.
A4: un transcript di un altro profilo ferma il dispatcher su OGNI evento.
A7: sotto un profilo non predefinito nessun interruttore spegne il firewall.
A8: un import rotto fallisce chiuso sotto un profilo non predefinito.

SC_PROFILE si imposta solo con monkeypatch.setenv o nel dizionario env dei
sottoprocessi, mai in un comando di shell.
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sc_hooks import core

HOOKS_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = HOOKS_DIR.parent / "lib"
PROFILES = ("superclaude", "deepseek")
OTHER_HOME = {"superclaude": ".claude-deepseek", "deepseek": ".claude"}
OWN_HOME = {"superclaude": ".claude", "deepseek": ".claude-deepseek"}
_SWITCHES = ("SUPERCLAUDE_GUARDS", "SUPERCLAUDE_GUARD_PROFILE_FIREWALL", "SUPERCLAUDE_GUARD_WRITE_ACL")


@pytest.fixture
def profile(request, home, monkeypatch):
    """Attiva il profilo richiesto nel processo dei test (costante di core + ambiente)."""
    name = getattr(request, "param", "superclaude")
    for var in _SWITCHES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(core, "PROFILE", name)
    if name == "superclaude":
        monkeypatch.delenv("SC_PROFILE", raising=False)
    else:
        monkeypatch.setenv("SC_PROFILE", name)
    return name


@pytest.fixture
def full_registry(monkeypatch):
    """Registro reale, come lo carica dispatch.py, ripristinato a fine test."""
    import sc_hooks.handlers as pkg
    for mod in sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)):
        importlib.import_module(f"sc_hooks.handlers.{mod}")
    monkeypatch.setattr(core, "_REGISTRY", list(core._REGISTRY))
    return core._REGISTRY


def _ctx(home: Path, event: str, tool: str = "", payload: dict | None = None, **ti) -> core.Ctx:
    return core.Ctx(event=event, payload=payload or {}, session_id="s1", tool=tool, tool_input=ti,
                    cwd=str(home / "code" / "proj"), role="plain", agent="")


# --------------------------------------------------------------------------
# A2: Monitor
# --------------------------------------------------------------------------
def test_shell_tools_is_exactly_bash_powershell_monitor():
    assert core.SHELL_TOOLS == ("Bash", "PowerShell", "Monitor")


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
def test_monitor_without_command_passes_untouched(profile, home, full_registry, capsys):
    ti = {"url": "ws://127.0.0.1:9000/stream", "description": "watch the socket"}
    ctx = _ctx(home, "PreToolUse", "Monitor", **dict(ti))
    assert core.dispatch(ctx) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert "updatedInput" not in out.out and "GUARD-WARN" not in out.out
    assert ctx.tool_input == ti


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
@pytest.mark.parametrize("tool", ["Bash", "Monitor"])
def test_monitor_command_is_firewalled_like_bash(profile, tool, home, full_registry, capsys):
    other = OTHER_HOME[profile]
    ctx = _ctx(home, "PreToolUse", tool, command=f"cat ~/{other}/agent-memory/.memory.db")
    assert core.dispatch(ctx) == 2
    assert "[profile-firewall]" in capsys.readouterr().err


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
def test_monitor_plain_command_is_allowed(profile, home, full_registry, capsys):
    ctx = _ctx(home, "PreToolUse", "Monitor", command="tail -f build.log", description="build")
    assert core.dispatch(ctx) == 0
    assert "GUARD-BLOCK" not in capsys.readouterr().err


# --------------------------------------------------------------------------
# A4: transcript di un altro profilo, ogni evento
# --------------------------------------------------------------------------
_EVENTS = ("SessionStart", "SubagentStart", "PostToolUse", "PreCompact", "Stop", "SubagentStop", "SessionEnd")


@pytest.fixture
def recorder(monkeypatch):
    called: list[str] = []
    reg = [(ev, (), f"rec-{ev}", lambda c, e=ev: called.append(e) or core.Result.note("x"))
           for ev in ("PreToolUse",) + _EVENTS]
    monkeypatch.setattr(core, "_REGISTRY", reg)
    return called


def _transcript(home: Path, root: str) -> str:
    path = home / root / "projects" / "c--x" / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
def test_foreign_transcript_denies_pretooluse(profile, home, recorder, capsys):
    raw = _transcript(home, OTHER_HOME[profile])
    ctx = _ctx(home, "PreToolUse", "Bash", {"transcript_path": raw}, command="echo hi")
    assert core.dispatch(ctx) == 2
    out = capsys.readouterr()
    assert out.err.startswith("GUARD-BLOCK [profile] fail closed: transcript_path")
    assert f"selects {profile}" in out.err and out.out == ""
    assert recorder == []


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
@pytest.mark.parametrize("event", _EVENTS)
def test_foreign_transcript_other_events_do_nothing(profile, event, home, recorder, capsys):
    raw = _transcript(home, OTHER_HOME[profile])
    assert core.dispatch(_ctx(home, event, "Agent", {"transcript_path": raw})) == 0
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")
    assert recorder == []


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
@pytest.mark.parametrize("event", ("PreToolUse",) + _EVENTS)
def test_own_or_external_transcript_runs_handlers(profile, event, home, recorder, tmp_path, capsys):
    for raw in (_transcript(home, OWN_HOME[profile]), str(tmp_path / "elsewhere" / "t.jsonl"), ""):
        recorder.clear()
        assert core.dispatch(_ctx(home, event, "Bash", {"transcript_path": raw}, command="echo")) == 0
        assert recorder == [event]
    capsys.readouterr()


@pytest.mark.parametrize("profile", PROFILES, indirect=True)
def test_foreign_transcript_msys_form_is_recognised(profile, home, recorder, capsys):
    raw = _transcript(home, OTHER_HOME[profile]).replace("\\", "/")
    if raw[1:2] == ":":
        raw = f"/{raw[0].lower()}{raw[2:]}"
    assert core.dispatch(_ctx(home, "PreToolUse", "Bash", {"transcript_path": raw}, command="ls")) == 2
    capsys.readouterr()


def test_foreign_transcript_via_junction_is_recognised(profile, home, recorder, capsys, tmp_path):
    # Gli hook girano solo su Windows nativo: la giunzione NTFS si crea senza privilegi
    real = home / ".claude-deepseek" / "projects"
    real.mkdir(parents=True)
    (real / "t.jsonl").write_text("{}\n", encoding="utf-8")
    link = tmp_path / "jn"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    ctx = _ctx(home, "PreToolUse", "Bash", {"transcript_path": str(link / "t.jsonl")}, command="ls")
    assert core.dispatch(ctx) == 2
    capsys.readouterr()


# --------------------------------------------------------------------------
# A7: interruttori e firewall
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", ["deepseek"], indirect=True)
@pytest.mark.parametrize("var,val", [("SUPERCLAUDE_GUARDS", "off"), ("SUPERCLAUDE_GUARD_PROFILE_FIREWALL", "off"),
                                     ("SUPERCLAUDE_GUARD_PROFILE_FIREWALL", "warn")])
def test_switches_cannot_weaken_firewall_off_default(profile, var, val, monkeypatch, home, full_registry, capsys):
    monkeypatch.setenv(var, val)
    assert core.guard_mode(core.PROFILE_FIREWALL) == "block"
    ctx = _ctx(home, "PreToolUse", "Bash", command="cat ~/.claude/agent-memory/.memory.db")
    assert core.dispatch(ctx) == 2
    assert "[profile-firewall]" in capsys.readouterr().err


@pytest.mark.parametrize("profile", ["deepseek"], indirect=True)
def test_global_off_still_switches_other_guards_off_default(profile, monkeypatch):
    monkeypatch.setenv("SUPERCLAUDE_GUARDS", "off")
    assert core.guard_mode("write-acl") == "off"


def test_switches_unchanged_in_default_profile(profile, monkeypatch):
    monkeypatch.setenv("SUPERCLAUDE_GUARDS", "off")
    assert core.guard_mode(core.PROFILE_FIREWALL) == "off"
    monkeypatch.delenv("SUPERCLAUDE_GUARDS")
    monkeypatch.setenv("SUPERCLAUDE_GUARD_PROFILE_FIREWALL", "warn")
    assert core.guard_mode(core.PROFILE_FIREWALL) == "warn"


def test_env_alone_marks_non_default(profile, monkeypatch):
    assert core.non_default_profile() is False
    monkeypatch.setenv("SC_PROFILE", "deepseek")
    assert core.non_default_profile() is True
    monkeypatch.setenv("SUPERCLAUDE_GUARDS", "off")
    assert core.guard_mode(core.PROFILE_FIREWALL) == "block"


# --------------------------------------------------------------------------
# A8: import rotti (copia dell'albero in tmp_path, sottoprocesso con -S)
# --------------------------------------------------------------------------
def _tree(tmp_path: Path, broken: str) -> Path:
    root = tmp_path / "tree"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(HOOKS_DIR, root / "hooks", ignore=ignore)
    shutil.copytree(LIB_DIR, root / "lib", ignore=ignore)
    target = root / ("lib/sc_profile.py" if broken == "lib" else "hooks/sc_hooks/handlers/profile_firewall.py")
    target.write_text('raise ImportError("broken on purpose")\n', encoding="utf-8")
    return root / "hooks" / "dispatch.py"


def _run(dispatch: Path, fh: Path, event: str, profile: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("SC_PROFILE", "SUPERCLAUDE_GUARDS")}
    env.update(USERPROFILE=str(fh), HOME=str(fh), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    if profile is not None:
        env["SC_PROFILE"] = profile
    payload = {"hook_event_name": event, "session_id": "s1", "cwd": str(fh), "tool_name": "Bash",
               "tool_input": {"command": "echo hi"}}
    return subprocess.run([sys.executable, "-S", str(dispatch), event], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


@pytest.fixture
def fh(tmp_path):
    h = tmp_path / "fh"
    h.mkdir()
    return h


@pytest.mark.parametrize("prof", ["deepseek", "DeepSeek", "bogus"])
def test_broken_lib_denies_pretooluse_off_default(tmp_path, fh, prof):
    proc = _run(_tree(tmp_path, "lib"), fh, "PreToolUse", prof)
    assert proc.returncode == 2
    assert proc.stderr.startswith("GUARD-BLOCK [profile] fail closed: the hook core failed to import")
    assert proc.stdout == ""


@pytest.mark.parametrize("prof", [None, "", "superclaude"])
def test_broken_lib_keeps_legacy_failure_in_default(tmp_path, fh, prof):
    proc = _run(_tree(tmp_path, "lib"), fh, "PreToolUse", prof)
    assert proc.returncode == 1 and "broken on purpose" in proc.stderr


def test_broken_lib_other_events_never_block(tmp_path, fh):
    proc = _run(_tree(tmp_path, "lib"), fh, "SessionStart", "deepseek")
    assert proc.returncode != 2 and "GUARD-BLOCK" not in proc.stderr


def test_broken_firewall_denies_pretooluse_off_default(tmp_path, fh):
    proc = _run(_tree(tmp_path, "firewall"), fh, "PreToolUse", "deepseek")
    assert proc.returncode == 2
    assert proc.stderr.startswith("GUARD-BLOCK [profile] fail closed: the profile firewall failed to import")


def test_broken_firewall_default_profile_unchanged(tmp_path, fh):
    proc = _run(_tree(tmp_path, "firewall"), fh, "PreToolUse", None)
    assert proc.returncode == 0 and "GUARD-BLOCK" not in proc.stderr


def test_broken_firewall_other_events_run(tmp_path, fh):
    proc = _run(_tree(tmp_path, "firewall"), fh, "Stop", "deepseek")
    assert proc.returncode == 0 and proc.stderr == ""


# --------------------------------------------------------------------------
# A4 end to end: PreCompact con il transcript dell'altro profilo non scrive nulla
# --------------------------------------------------------------------------
def test_precompact_foreign_transcript_writes_nothing(fh):
    transcript = fh / ".claude" / "projects" / "c--x" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    before = sorted(p for p in fh.rglob("*"))
    env = {k: v for k, v in os.environ.items() if k not in ("SC_PROFILE", "SUPERCLAUDE_GUARDS")}
    env.update(USERPROFILE=str(fh), HOME=str(fh), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               SC_PROFILE="deepseek")
    for event in ("PreCompact", "SessionStart", "Stop", "SessionEnd", "PreToolUse"):
        payload = {"hook_event_name": event, "session_id": "s1", "cwd": str(fh), "tool_name": "Bash",
                   "tool_input": {"command": "echo hi"}, "transcript_path": str(transcript), "trigger": "auto"}
        proc = subprocess.run([sys.executable, "-S", str(HOOKS_DIR / "dispatch.py"), event],
                              input=json.dumps(payload), env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        if event == "PreToolUse":
            assert proc.returncode == 2 and "belongs to the superclaude profile" in proc.stderr
        else:
            assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), event
    assert sorted(p for p in fh.rglob("*")) == before
