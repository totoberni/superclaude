"""Fixture condivise dai test degli handler: una HOME finta sotto tmp_path.

Nessun test tocca la vera ~/.claude: le costanti di `core` che dipendono dalla
HOME vengono ripuntate, e USERPROFILE/HOME cambiano così anche `~` e `$HOME`
si espandono nella HOME finta.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sc_hooks import core  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "config").mkdir(parents=True)
    (h / "code" / "proj").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("SUPERCLAUDE_GIT_POLICY_FILE", raising=False)
    monkeypatch.delenv("SC_PROFILE", raising=False)
    monkeypatch.setattr(core, "CLAUDE_HOME", h / ".claude")
    monkeypatch.setattr(core, "CODE_HOME", h / ".claude")
    monkeypatch.setattr(core, "DATA_HOME", h / ".claude")
    monkeypatch.setattr(core, "STATE_DIR", h / ".claude" / "state")
    monkeypatch.setattr(core, "LOG_DIR", h / ".claude" / "logs")
    monkeypatch.setattr(core, "TMP_DIR", h / ".claude" / "tmp")
    monkeypatch.setattr(core, "WORKSPACE_ROOT", h / "code")
    return h


def win(p) -> str:
    """Forma Windows con backslash."""
    return str(p).replace("/", "\\")


def msys(p) -> str:
    """Forma Git Bash: /c/Users/..."""
    s = str(p).replace("\\", "/")
    return f"/{s[0].lower()}{s[2:]}" if len(s) > 1 and s[1] == ":" else s


@pytest.fixture
def mk(home):
    """Costruttore di Ctx: mk(tool, role="plain", event=..., cwd=..., **tool_input)."""
    def _mk(tool, role="plain", event="PreToolUse", cwd=None, agent=None, tool_input=None, **ti):
        return core.Ctx(
            event=event, payload={}, session_id="test-session", tool=tool,
            tool_input=ti if tool_input is None else tool_input,
            cwd=str(home / "code" / "proj") if cwd is None else cwd,
            role=role, agent=agent if agent is not None else {"worker": "w-coder", "meta": "meta"}.get(role, ""),
        )
    return _mk
