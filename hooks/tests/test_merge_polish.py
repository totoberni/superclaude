"""Rifiniture applicate da meta al merge: cd-note ignora Monitor; errore a runtime chiude nel profilo isolato."""
from __future__ import annotations

import importlib
import sys

import pytest

from sc_hooks import core


def _ctx(tool: str, command: str, cwd: str) -> core.Ctx:
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"command": command}, "cwd": cwd}
    return core.Ctx(event="PreToolUse", payload=payload, session_id="t", tool=tool,
                    tool_input={"command": command}, cwd=cwd, role="plain", agent="")


def test_cd_note_ignores_monitor(home):
    cd_note = importlib.import_module("sc_hooks.handlers.cd_note")
    cwd = str(home / "code")
    assert cd_note.handle(_ctx("Monitor", "cd C:/Windows && dir", cwd)) is None


@pytest.mark.parametrize("profile_value, expected", [("deepseek", 2), ("", 0), ("superclaude", 0)])
def test_runtime_error_fails_closed_only_under_non_default(home, monkeypatch, capsys,
                                                            profile_value, expected):
    dispatch = importlib.import_module("dispatch")
    monkeypatch.setenv("SC_PROFILE", profile_value)

    def boom(_event):
        raise RuntimeError("simulated dispatcher bug")

    monkeypatch.setattr(dispatch.core, "read_ctx", boom)
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "PreToolUse"])
    assert dispatch.main() == expected
    if expected == 2:
        assert "fail closed" in capsys.readouterr().err


def test_runtime_error_other_events_stay_open(home, monkeypatch):
    dispatch = importlib.import_module("dispatch")
    monkeypatch.setenv("SC_PROFILE", "deepseek")
    monkeypatch.setattr(dispatch.core, "read_ctx", lambda _e: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "SessionStart"])
    assert dispatch.main() == 0
