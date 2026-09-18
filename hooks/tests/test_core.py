"""Test del nucleo degli hook: normalizzazione percorsi, identità, dispatch."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sc_hooks import core  # noqa: E402

HOME = str(Path.home()).replace("\\", "/").lower()


@pytest.mark.parametrize("raw", [
    r"C:\Users\x\.claude\settings.json",
    "C:/Users/x/.claude/settings.json",
    "/c/Users/x/.claude/settings.json",
    "c:/users/X/.CLAUDE/./hooks/../settings.json",
])
def test_norm_path_equivalent_spellings(raw):
    assert core.norm_path(raw) == "c:/users/x/.claude/settings.json"


def test_norm_path_home_forms():
    for raw in ("~/.claude/x.md", "$HOME/.claude/x.md", "%USERPROFILE%/.claude/x.md"):
        assert core.norm_path(raw) == f"{HOME}/.claude/x.md"


def test_norm_path_relative_uses_cwd():
    assert core.norm_path("src/a.py", cwd=r"C:\Users\x\code\proj") == "c:/users/x/code/proj/src/a.py"
    assert core.norm_path("../b.py", cwd="/c/Users/x/code/proj") == "c:/users/x/code/b.py"


def test_is_under_and_rel_to():
    assert core.is_under(r"C:\Users\x\.claude\plans\p\plan.md", "c:/users/x/.claude")
    assert not core.is_under(r"C:\Users\x\.claudeX\a", "c:/users/x/.claude")   # niente falsi prefissi
    assert core.rel_to("/c/Users/x/.claude/plans/p/plan.md", "C:/Users/x/.claude") == "plans/p/plan.md"
    assert core.rel_to("C:/elsewhere/a", "C:/Users/x/.claude") is None


def test_identity(monkeypatch):
    monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
    assert core.resolve_identity({}) == ("plain", "")
    assert core.resolve_identity({"agent_type": "w-reviewer"}) == ("worker", "w-reviewer")
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "meta")
    assert core.resolve_identity({}) == ("meta", "meta")
    # un subagent lanciato da meta eredita l'env ma resta un worker
    assert core.resolve_identity({"agent_type": "w-tester"}) == ("worker", "w-tester")


def _ctx(event="PreToolUse", tool="Write", **ti):
    return core.Ctx(event=event, payload={}, session_id="t", tool=tool, tool_input=ti, cwd="C:/x", role="plain", agent="")


def test_dispatch_aggregates_and_blocks(monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [])
    core.on("PreToolUse", tools=("Write",), name="a")(lambda c: core.Result.note("nota-a"))
    core.on("PreToolUse", tools=("Write",), name="b")(lambda c: core.Result.deny("vietato"))
    core.on("PreToolUse", tools=("Bash",), name="c")(lambda c: core.Result.deny("non deve scattare"))
    assert core.dispatch(_ctx()) == 2
    err = capsys.readouterr().err
    assert "vietato" in err and "non deve scattare" not in err


def test_dispatch_single_json_and_fail_open(monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [])
    monkeypatch.setattr(core, "log_error", lambda *a: None)

    def boom(_):
        raise RuntimeError("bug")

    core.on("PostToolUse", name="x")(boom)
    core.on("PostToolUse", name="y")(lambda c: core.Result.note("uno"))
    core.on("PostToolUse", name="z")(lambda c: core.Result.deny("post non blocca mai"))
    assert core.dispatch(_ctx(event="PostToolUse")) == 0
    out = json.loads(capsys.readouterr().out)          # un solo oggetto JSON valido
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "uno" in text and "GUARD-WARN [z]" in text


def test_guard_mode_switches(monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [])
    core.on("PreToolUse", name="write-acl")(lambda c: core.Result.deny("no"))
    monkeypatch.setenv("SUPERCLAUDE_GUARD_WRITE_ACL", "warn")
    assert core.dispatch(_ctx()) == 0
    monkeypatch.setenv("SUPERCLAUDE_GUARDS", "off")
    capsys.readouterr()
    assert core.dispatch(_ctx()) == 0 and capsys.readouterr().out == ""


def test_read_ctx_handles_bom_and_garbage(monkeypatch):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": "s1",
               "tool_input": {"file_path": r"C:\a\b.py", "new_string": "x"}}
    raw = b"\xef\xbb\xbf" + json.dumps(payload).encode()
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(raw)})())
    ctx = core.read_ctx(None)
    assert ctx.tool == "Edit" and ctx.file_path == r"C:\a\b.py" and ctx.new_content == "x"
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b"not json")})())
    assert core.read_ctx("Stop").event == "Stop"
