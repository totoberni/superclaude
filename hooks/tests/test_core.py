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


def test_norm_path_expands_8dot3_short_names(tmp_path):
    """I nomi corti 8.3 devono confrontarsi uguali alla forma lunga (solo Windows)."""
    import os
    if os.name != "nt":
        pytest.skip("solo Windows")
    import ctypes
    long_dir = tmp_path / "a long directory name"
    long_dir.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buf, 1024) or "~" not in buf.value:
        pytest.skip("nomi 8.3 disabilitati su questo volume")
    short = buf.value
    assert core.norm_path(short) == core.norm_path(str(long_dir))
    # vale anche per un file che non esiste ancora sotto la cartella corta
    assert core.norm_path(short + r"\new\file.txt") == core.norm_path(str(long_dir / "new" / "file.txt"))
    assert core.is_under(short + r"\x.md", str(long_dir))


def test_norm_path_and_ctx_tolerate_bad_types():
    assert core.norm_path(None) == "" and core.norm_path(b"bytes") == ""
    assert core.norm_path("a.py", cwd=123).endswith("/a.py")
    ctx = core.Ctx(event="PreToolUse", payload={}, session_id="t", tool="Edit",
                   tool_input={"file_path": 42, "edits": "not-a-list", "command": ["x"], "content": None},
                   cwd="C:/x", role="plain", agent="")
    assert ctx.file_path == "" and ctx.command == "" and ctx.new_content == ""


def test_default_mode_warn_never_blocks(monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [])
    monkeypatch.setattr(core, "_DEFAULT_MODE", {})
    core.on("PreToolUse", name="advisory", default_mode="warn")(lambda c: core.Result.deny("heads up"))
    assert core.dispatch(_ctx()) == 0
    assert "GUARD-WARN [advisory]" in capsys.readouterr().out
    monkeypatch.setenv("SUPERCLAUDE_GUARD_ADVISORY", "block")     # l'ambiente può inasprire
    assert core.dispatch(_ctx()) == 2


@pytest.mark.parametrize("sid", ["..", ".", "...", "../..", "", "..\\..", "/", "a/../.."])
def test_state_file_never_escapes_state_dir(sid):
    """Un session_id ostile non deve mai far uscire lo stato da STATE_DIR/<nome>/."""
    ctx = core.Ctx(event="SessionEnd", payload={}, session_id=sid, tool="", tool_input={},
                   cwd="C:/x", role="plain", agent="")
    target = ctx.state_file("..")
    assert target.parent.parent == core.STATE_DIR
    assert target.parent.name not in ("", ".", "..") and target.name not in ("", ".", "..")


def test_fs_path_keeps_case_and_resolves_forms():
    assert core.fs_path(None) is None and core.fs_path("") is None
    assert str(core.fs_path("/c/Users/X/Proj/A.py")).replace("\\", "/") == "C:/Users/X/Proj/A.py"
    assert str(core.fs_path("src/A.py", cwd="/c/Users/X/Proj")).replace("\\", "/") == "C:/Users/X/Proj/src/A.py"
    assert core.fs_path("~/x.md") == Path.home() / "x.md"


def test_dispatch_emits_full_updated_input_and_later_guards_see_it(monkeypatch, capsys):
    """Una riscrittura arriva come `updatedInput` completo e le guardie successive la vedono."""
    monkeypatch.setattr(core, "_REGISTRY", [])
    seen = {}
    core.on("PreToolUse", name="a-fix")(lambda c: core.Result.rewrite({**c.tool_input, "content": "clean"}, "fixed it"))
    core.on("PreToolUse", name="b-look")(lambda c: seen.setdefault("content", c.tool_input["content"]) and None)
    assert core.dispatch(_ctx(file_path="C:/x/a.md", content="dirty")) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["updatedInput"] == {"file_path": "C:/x/a.md", "content": "clean"}
    assert out["additionalContext"] == "fixed it" and seen["content"] == "clean"
    assert "permissionDecision" not in out          # la riscrittura non deve aggirare i permessi


def test_rewrite_is_ignored_outside_pretooluse(monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [])
    core.on("PostToolUse", name="late")(lambda c: core.Result.rewrite({"content": "x"}))
    assert core.dispatch(_ctx(event="PostToolUse")) == 0
    assert capsys.readouterr().out == ""


def test_block_reports_discarded_rewrites(monkeypatch, capsys):
    """Un blocco scarta le correzioni automatiche: lo stderr deve dirlo, con le note di riscrittura."""
    monkeypatch.setattr(core, "_REGISTRY", [])
    core.on("PreToolUse", name="a-fix")(lambda c: core.Result.rewrite({**c.tool_input, "content": "clean"}, "dash fixed"))
    core.on("PreToolUse", name="b-info")(lambda c: core.Result.note("solo informativa"))
    core.on("PreToolUse", name="c-deny")(lambda c: core.Result.deny("vietato"))
    assert core.dispatch(_ctx(file_path="C:/x/a.md", content="dirty")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "GUARD-BLOCK [c-deny] vietato" in captured.err
    assert "auto-fixes in this call were not applied" in captured.err and "reapply automatically on retry" in captured.err
    assert "dash fixed" in captured.err and "solo informativa" not in captured.err


def test_block_without_rewrites_has_no_autofix_line(monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [])
    core.on("PreToolUse", name="b-info")(lambda c: core.Result.note("solo informativa"))
    core.on("PreToolUse", name="c-deny")(lambda c: core.Result.deny("vietato"))
    assert core.dispatch(_ctx()) == 2
    err = capsys.readouterr().err
    assert "vietato" in err and "auto-fixes" not in err
