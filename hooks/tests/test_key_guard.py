"""Test della guardia key-guard: nessun agente tocca per nome la chiave del vault della memoria.

Solo stringhe: nessun test crea, apre o legge un file chiave.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sc_hooks import core
from sc_hooks.handlers import key_guard

ROLES = ("plain", "meta", "worker")
SHELLS = ("Bash", "PowerShell", "Monitor")

KEY_PATHS = [
    "C:\\Users\\someone\\.claude\\agent-memory\\.memsync.key",
    "C:/Users/someone/.claude/agent-memory/.memsync.key",
    "/c/Users/someone/.claude/agent-memory/.memsync.key",
    "~/.claude/agent-memory/.memsync.key",
    "$env:USERPROFILE\\.claude\\agent-memory\\.MEMSYNC.KEY",
    "agent-memory/.Memsync.Key",
    ".memsync.key",
    "D:/backup/.memsync.key.bak",
]

# Strumento e campo percorso che lo strumento usa davvero
PATH_FIELDS = [
    ("Read", "file_path"), ("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
    ("NotebookEdit", "notebook_path"), ("Glob", "path"), ("Grep", "path"),
]

KEY_COMMANDS = [
    "cat ~/.claude/agent-memory/.memsync.key",
    "xxd /c/Users/someone/.claude/agent-memory/.MEMSYNC.KEY | head",
    "cp agent-memory/.memsync.key /tmp/k",
    "Get-Content $env:USERPROFILE\\.claude\\agent-memory\\.memsync.key",
    "Format-Hex -Path 'C:\\Users\\someone\\.claude\\agent-memory\\.memsync.key'",
    "base64 < .memsync.key",
    "gpg --symmetric --output k.asc .memsync.key",
    "cd ~/.claude/agent-memory && tail -f .memsync.key",
    'bash -c "od -x .Memsync.Key"',
]

UNRELATED_PATHS = [
    "C:/Users/someone/.claude/agent-memory/.memsync.lock",
    "C:/Users/someone/.claude/agent-memory/.memsync-state.json",
    "C:/Users/someone/.claude/agent-memory/supermemdb.git/config",
    "C:/Users/someone/Downloads/memsync-sc-v1.key.asc",
    "C:/Users/someone/.claude/scripts/memory/memvault.py",
    "C:/Users/someone/.claude/agent-memory/.memory.db",
]

UNRELATED_COMMANDS = [
    "ls ~/.claude/agent-memory", "mem sync status", "git -C ~/.claude status",
    "Get-ChildItem $env:USERPROFILE\\.claude\\agent-memory", "echo memsync key", "",
]


def denied(res):
    return res is not None and bool(res.block)


@pytest.mark.parametrize("tool,field", PATH_FIELDS)
@pytest.mark.parametrize("path", KEY_PATHS)
def test_path_field_naming_the_key_is_denied(mk, tool, field, path):
    for role in ROLES:
        res = key_guard.handle(mk(tool, role=role, **{field: path}))
        assert denied(res) and "memsync.key" in res.block, (tool, field, role)


@pytest.mark.parametrize("field", ["file_path", "path", "notebook_path"])
def test_every_path_field_is_scanned_on_any_tool(mk, field):
    assert denied(key_guard.handle(mk("Read", **{field: KEY_PATHS[0]})))


@pytest.mark.parametrize("glob", ["**/.memsync.key", ".memsync.key", "*.MEMSYNC.KEY", "{a,.memsync.key}"])
def test_grep_glob_naming_the_key_is_denied(mk, glob):
    res = key_guard.handle(mk("Grep", pattern=".", path="C:/Users/someone/.claude", glob=glob))
    assert denied(res)


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", KEY_COMMANDS)
def test_shell_text_naming_the_key_is_denied(mk, tool, cmd):
    for role in ROLES:
        assert denied(key_guard.handle(mk(tool, role=role, command=cmd))), (tool, role, cmd)


def test_denied_in_every_profile(mk, monkeypatch):
    monkeypatch.setenv("SC_PROFILE", "deepseek")
    assert denied(key_guard.handle(mk("Read", file_path=KEY_PATHS[1])))
    assert denied(key_guard.handle(mk("Bash", command=KEY_COMMANDS[0])))


# --------------------------------------------------------------------------
# Campi che la guardia non esamina
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", ["**/.memsync.key", "**/*.memsync.key", ".MEMSYNC.KEY"])
def test_glob_pattern_naming_the_key_is_allowed(mk, pattern):
    assert key_guard.handle(mk("Glob", pattern=pattern)) is None
    assert key_guard.handle(mk("Glob", pattern=pattern, path="C:/Users/someone/.claude/agent-memory")) is None


@pytest.mark.parametrize("pattern", ["memsync\\.key", "memsync.key", ".MEMSYNC.KEY"])
def test_grep_pattern_naming_the_key_is_allowed(mk, pattern):
    assert key_guard.handle(mk("Grep", pattern=pattern)) is None
    assert key_guard.handle(mk("Grep", pattern=pattern, path="C:/Users/someone/.claude/scripts", glob="*.py")) is None


def test_written_content_is_not_scanned(mk):
    target = "C:/Users/someone/.claude/docs/memory-vault.md"
    text = "The key lives at <agent-memory>/.memsync.key"
    assert key_guard.handle(mk("Write", file_path=target, content=text)) is None
    assert key_guard.handle(mk("Edit", file_path=target, old_string="x", new_string=text)) is None
    assert key_guard.handle(mk("MultiEdit", file_path=target, edits=[{"old_string": "x", "new_string": text}])) is None
    assert key_guard.handle(mk("NotebookEdit", notebook_path="C:/x/n.ipynb", new_source=text)) is None


def test_agent_prompt_is_not_scanned(mk):
    assert key_guard.handle(mk("Agent", prompt="cat .memsync.key", description="x")) is None
    tools = {t for ev, ts, name, _ in core._REGISTRY if name == key_guard.NAME for t in ts}
    assert "Agent" not in tools


@pytest.mark.parametrize("tool,field", PATH_FIELDS)
@pytest.mark.parametrize("path", UNRELATED_PATHS)
def test_unrelated_paths_are_allowed(mk, tool, field, path):
    assert key_guard.handle(mk(tool, **{field: path})) is None


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", UNRELATED_COMMANDS)
def test_unrelated_commands_are_allowed(mk, tool, cmd):
    assert key_guard.handle(mk(tool, command=cmd)) is None


@pytest.mark.parametrize("ti", [{}, {"file_path": None}, {"path": 42}, {"glob": ["**/.memsync.key"]},
                                {"command": ["cat", ".memsync.key"]}, {"notebook_path": b".memsync.key"}])
def test_malformed_input_is_ignored(mk, ti):
    for tool in ("Read", "Grep", "Bash"):
        assert key_guard.handle(mk(tool, tool_input=ti)) is None
    assert key_guard.handle(mk("Read", tool_input=".memsync.key")) is None


# --------------------------------------------------------------------------
# Registrazione e dispatcher
# --------------------------------------------------------------------------
def test_registered_on_shell_write_and_read_tools():
    entries = [(ev, ts) for ev, ts, name, _ in core._REGISTRY if name == key_guard.NAME]
    assert len(entries) == 1
    event, tools = entries[0]
    assert event == "PreToolUse"
    assert set(tools) == set(core.SHELL_TOOLS) | set(core.WRITE_TOOLS) | {"Read", "Glob", "Grep"}


@pytest.mark.parametrize("tool,ti", [
    ("Read", {"file_path": KEY_PATHS[0]}),
    ("Grep", {"pattern": ".", "glob": "**/.memsync.key"}),
    ("PowerShell", {"command": KEY_COMMANDS[3]}),
])
def test_dispatch_blocks_with_the_guard_name(mk, capsys, tool, ti):
    assert core.dispatch(mk(tool, tool_input=dict(ti))) == 2
    assert "[key-guard]" in capsys.readouterr().err


def test_source_has_no_forbidden_dash():
    text = Path(key_guard.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text
