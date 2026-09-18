"""Test di spawn-log: layout TSV dei tre file, classificazione, durata, input sporco."""
from __future__ import annotations

import re

import pytest

from sc_hooks import core
from sc_hooks.handlers import spawn_log as sl

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@pytest.fixture
def comms(home, monkeypatch):
    d = home / ".claude" / "comms"
    monkeypatch.setattr(sl, "COMMS_DIR", d)
    return d


def _ctx(event, payload=None, agent="meta", **ti):
    return core.Ctx(event=event, payload=payload or {}, session_id="s1", tool="Agent", tool_input=ti, cwd="C:/x",
                    role="meta" if agent == "meta" else "plain", agent=agent)


def _rows(path):
    return [ln.split("\t") for ln in path.read_text(encoding="utf-8").split("\n") if ln and not ln.startswith("#")]


def test_pre_row_layout_and_sanitising(comms):
    sl.on_spawn(_ctx("PreToolUse", subagent_type="w-tester", description="run\tthe\nsuite " + "x" * 200))
    sl.on_spawn(_ctx("PreToolUse", agent=""))
    rows = _rows(comms / "_spawns.log")
    assert len(rows[0]) == 4 and TS.match(rows[0][0])
    assert rows[0][1:3] == ["meta", "w-tester"] and rows[0][3].startswith("runthesuite ") and len(rows[0][3]) <= 80
    assert rows[1][1:] == ["unknown", "unknown", ""]


def test_post_writes_rich_spawn_and_outcome_with_header_once(comms):
    resp = {"agentId": "a1c9-61bf!!", "status": "async_launched", "content": [{"type": "text", "text": "## Summary\nall good."}]}
    for _ in range(2):
        sl.on_outcome(_ctx("PostToolUse", payload={"tool_response": resp}, subagent_type="w-coder",
                           description="d" * 130))
    rich = _rows(comms / "_spawns-rich.log")
    assert len(rich[0]) == 6 and rich[0][1:] == ["a1c9-61bf", "w-coder", "SPAWN", "d" * 40, ""]
    raw = (comms / "_outcomes.log").read_text(encoding="utf-8")
    assert raw.count("# Outcomes:") == 1 and raw.startswith("# Agent outcome log")
    out = _rows(comms / "_outcomes.log")
    assert len(out) == 2 and len(out[0]) == 6
    assert out[0][1:5] == ["meta", "w-coder", "d" * 120, "OK"] and out[0][5] == "## Summaryall good."


@pytest.mark.parametrize("resp,expected", [
    ("plain string answer", "plain string answer"),
    ([{"type": "text", "text": "a"}, "b", {"k": 1}], 'a\nb\n{"k": 1}'),
    ({"content": "c"}, "c"),
    ({"content": [{"text": "x"}, {"text": "y"}]}, "x\ny"),
    ({"text": "t"}, "t"),
    ({"output": {"text": "deep"}}, "deep"),
    ({"content": 7}, "7"),
    (None, ""),
    (5, "5"),
])
def test_response_text_shapes(resp, expected):
    assert sl.response_text(resp) == expected


def test_response_text_dict_fallback_is_json():
    assert '"agentId"' in sl.response_text({"agentId": "x", "status": "async_launched"})


@pytest.mark.parametrize("text,expected", [
    ("", "UNKNOWN"),
    ("42", "OK"),
    ("work work\n## Verification\nok", "OK"),
    ("x" * 100 + "\nError: boom", "FAILED"),
    ("## Summary\nfine\nTraceback (most recent call last):", "FAILED"),
    ("I looked at the files and found several things. " * 3 + "Let me check the other module", "TRUNCATED"),
    ("I looked at the files and found several things. " * 3 + "and then the", "TRUNCATED"),
    ("I looked at the files and found several things. " * 3, "OK"),
    ("Error: early failure\n" + "y" * 3000 + ".", "OK"),                   # errore fuori dagli ultimi 2 KB
])
def test_classify(text, expected):
    assert sl.classify(text) == expected


def test_classify_huge_output_inspects_tail_only():
    assert sl.classify("z" * 60000 + " done.") == "OK"
    assert sl.classify("## Summary\n" + "z" * 60000 + " and then the") == "TRUNCATED"   # marcatore fuori dalla coda
    assert sl.classify("z" * 60000 + "\nfatal: not a git repository") == "FAILED"


def test_subagent_stop_rows_and_duration(comms, monkeypatch):
    stamps = iter(["2026-01-01T10:00:00Z", "2026-01-01T10:02:05Z"])
    monkeypatch.setattr(sl, "_now", lambda: next(stamps))
    sl.on_outcome(_ctx("PostToolUse", payload={"tool_response": {"agentId": "abc123"}}, subagent_type="w-coder",
                       description="job"))
    sl.on_stop(_ctx("SubagentStop", agent="w-coder", payload={
        "agent_id": "abc123", "agent_type": "w-co\tder", "last_assistant_message": "All done.",
        "agent_transcript_path": "C:\\t\\a.jsonl\n"}))
    spawns = _rows(comms / "_spawns.log")
    assert spawns[0] == ["2026-01-01T10:02:05Z", "", "w-coder", "EXIT", "abc123"]
    assert spawns[1] == ["2026-01-01T10:02:05Z", "", "w-coder", "STOP", "abc123", "125", "ok", "C:\\t\\a.jsonl"]
    rich = _rows(comms / "_spawns-rich.log")
    assert rich[-1] == ["2026-01-01T10:02:05Z", "abc123", "w-coder", "EXIT", "", "ok"]


def test_subagent_stop_fail_and_missing_fields(comms):
    sl.on_stop(_ctx("SubagentStop", payload={"last_assistant_message": "### Blockers\n- none of it worked"}))
    sl.on_stop(_ctx("SubagentStop", payload={"agent_id": 12, "agent_type": None, "last_assistant_message": ["x"]}))
    spawns = _rows(comms / "_spawns.log")
    assert spawns[0] == [spawns[0][0], "", "unknown", "EXIT", ""]
    assert spawns[1][2:] == ["unknown", "STOP", "unknown", "", "fail", ""]
    assert spawns[3][2:] == ["unknown", "STOP", "unknown", "", "", ""]


def test_duration_helpers(comms):
    assert sl._duration_s("2026-01-01T10:00:00Z", "2026-01-02T10:00:01Z") == "86401"
    assert sl._duration_s("2026-01-01T10:00:05Z", "2026-01-01T10:00:00Z") == ""     # orologio all'indietro
    assert sl._duration_s("garbage", "2026-01-01T10:00:00Z") == ""
    assert sl._spawn_ts(comms / "missing.log", "x") == ""


def test_fs_errors_are_swallowed(home, monkeypatch):
    blocker = home / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setattr(sl, "COMMS_DIR", blocker / "comms")
    assert sl.on_spawn(_ctx("PreToolUse", subagent_type="w")) is None
    assert sl.on_outcome(_ctx("PostToolUse", payload={"tool_response": "x"})) is None
    assert sl.on_stop(_ctx("SubagentStop", payload={"agent_id": "a"})) is None
