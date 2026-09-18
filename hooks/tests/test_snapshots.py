"""Test di snapshots: contenuto dello snapshot di compact, rotazione, snapshot di Stop."""
from __future__ import annotations

import json
import os
import time

import pytest

from conftest import msys
from sc_hooks import core
from sc_hooks.handlers import snapshots as sn


@pytest.fixture
def env(home, monkeypatch):
    c = home / ".claude"
    monkeypatch.delenv("COMPACT_SNAPSHOT_DIR", raising=False)
    monkeypatch.setattr(sn, "COMPACT_DIR", c / "agent-memory" / "_system" / "_compact-snapshots")
    monkeypatch.setattr(sn, "STOP_DIR", c / "agent-memory" / "_system" / "_stop-snapshots")
    monkeypatch.setattr(sn, "PLANS_DIR", c / "plans")
    monkeypatch.setattr(sn, "SPAWN_LOG", c / "comms" / "_spawns.log")
    monkeypatch.setattr(sn, "EPHEMERAL_DIR", c / "agents" / "_ephemeral")
    return c


def _ctx(event, sid="abcdef1234567890", **payload):
    return core.Ctx(event=event, payload=payload, session_id=sid, tool="", tool_input={}, cwd="C:/x",
                    role="meta", agent="meta")


def _transcript(path):
    entries = [
        {"type": "user", "message": {"content": "first   question"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/p/a.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/p/b.py"}}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "second\nquestion"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ignored"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "C:/p/a.py"}},
            {"type": "tool_use", "name": "Read", "input": {"notebook_path": "C:/p/n.ipynb"}},
            {"type": "tool_use", "name": "Read", "input": "bad"}]}},
        "a bare string", {"type": "assistant", "message": None},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json at all\n", encoding="utf-8")


def test_transcript_activity(tmp_path):
    t = tmp_path / "t.jsonl"
    _transcript(t)
    last_user, files = sn.transcript_activity(t)
    assert last_user == "second question"
    assert files == ["C:/p/n.ipynb", "C:/p/a.py", "C:/p/b.py"]           # il più recente per primo
    assert sn.transcript_activity(tmp_path / "missing.jsonl") == ("", [])


def test_tail_lines_bounded_read(tmp_path):
    f = tmp_path / "big.log"
    f.write_text("".join(f"line-{i}\n" for i in range(5000)), encoding="utf-8")
    assert sn._tail_lines(f, 3) == ["line-4997", "line-4998", "line-4999"]
    tail = sn._tail_lines(f, 10000, max_bytes=1000)
    assert tail[-1] == "line-4999" and len(tail) < 200 and all(ln.startswith("line-") and len(ln) > 8 for ln in tail)


def test_pre_compact_snapshot_content(env, tmp_path):
    plan = env / "plans" / "big-plan"
    plan.mkdir(parents=True)
    (plan / "state.md").write_text("".join(f"row {i}\n" for i in range(40)), encoding="utf-8")
    (plan / "plan.md").write_text("not a state file", encoding="utf-8")
    (env / "comms").mkdir()
    (env / "comms" / "_spawns.log").write_text("ts\tmeta\tw-coder\tjob\n", encoding="utf-8")
    t = tmp_path / "t.jsonl"
    _transcript(t)
    assert sn.on_pre_compact(_ctx("PreCompact", trigger="auto", transcript_path=msys(t))) is None
    snaps = list(sn.COMPACT_DIR.glob("compact-*.md"))
    assert len(snaps) == 1 and snaps[0].name.endswith("-abcdef12.md")
    text = snaps[0].read_text(encoding="utf-8")
    assert "**Session**: abcdef1234567890" in text and "**Agent**: meta" in text and "**Trigger**: auto" in text
    assert "### big-plan/state.md" in text and "row 19" in text and "row 20" not in text
    assert "not a state file" not in text
    assert "**Last user message**: second question" in text and "  - C:/p/n.ipynb" in text
    assert "w-coder\tjob" in text
    assert "orch" not in text.lower() and "directive" not in text.lower()


def test_pre_compact_rotation_and_env_override(env, tmp_path, monkeypatch):
    target = tmp_path / "override"
    target.mkdir()
    monkeypatch.setenv("COMPACT_SNAPSHOT_DIR", str(target))
    for i in range(7):
        old = target / f"compact-20200101-00000{i}-aaaaaaaa.md"
        old.write_text("x")
        os.utime(old, (1000 + i, 1000 + i))
    (target / "unrelated.md").write_text("keep")
    sn.on_pre_compact(_ctx("PreCompact", transcript_path=None, trigger=None))
    names = sorted(p.name for p in target.glob("compact-*.md"))
    assert len(names) == 5 and "compact-20200101-000000-aaaaaaaa.md" not in names
    assert "compact-20200101-000006-aaaaaaaa.md" in names and (target / "unrelated.md").exists()
    assert not sn.COMPACT_DIR.exists()


def test_pre_compact_bad_payload(env):
    ctx = _ctx("PreCompact", transcript_path=123, trigger=["x"])
    ctx.session_id = ""
    assert sn.on_pre_compact(ctx) is None
    assert len(list(sn.COMPACT_DIR.glob("compact-*-unknown.md"))) == 1


def test_stop_skips_when_nothing_to_save(env):
    assert sn.on_stop(_ctx("Stop")) is None
    assert not sn.STOP_DIR.exists()
    ctx = _ctx("Stop")
    ctx.session_id = ""
    assert sn.on_stop(ctx) is None


def test_stop_snapshot_dedupe_and_rotation(env, monkeypatch):
    (env / "comms").mkdir()
    log = env / "comms" / "_spawns.log"
    log.write_text("".join(f"row{i}\n" for i in range(150)), encoding="utf-8")
    eph = env / "agents" / "_ephemeral"
    eph.mkdir(parents=True)
    (eph / "w-temp.md").write_text("agent", encoding="utf-8")

    sn.on_stop(_ctx("Stop", sid="sess"))
    sn.on_stop(_ctx("Stop", sid="sess"))                         # sorgenti invariate: nessun duplicato
    snaps = [p for p in sn.STOP_DIR.iterdir()]
    assert len(snaps) == 1 and snaps[0].name.startswith("sess-")
    lines = (snaps[0] / "recent-spawns.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100 and lines[-1] == "row149"
    assert (snaps[0] / "_ephemeral" / "w-temp.md").read_text(encoding="utf-8") == "agent"

    now = time.time()
    for i in range(6):                                           # vecchi snapshot della stessa sessione
        d = sn.STOP_DIR / f"sess-2026010{i}-000000"
        d.mkdir()
        os.utime(d, (now - 1000 + i, now - 1000 + i))
    other = sn.STOP_DIR / "sess-other-20260101-000000"           # altra sessione: non ruota con questa
    other.mkdir()
    ancient = sn.STOP_DIR / "gone-20200101-000000"
    ancient.mkdir()
    os.utime(ancient, (now - 30 * 86400, now - 30 * 86400))
    log.write_text("changed\n", encoding="utf-8")
    sn.on_stop(_ctx("Stop", sid="sess"))
    names = sorted(p.name for p in sn.STOP_DIR.iterdir())
    assert "gone-20200101-000000" not in names and "sess-other-20260101-000000" in names
    mine = [n for n in names if n != "sess-other-20260101-000000"]
    assert len(mine) == 5 and "sess-20260100-000000" not in mine
