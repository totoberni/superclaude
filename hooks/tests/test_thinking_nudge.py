"""Test di thinking-nudge: matrice dei worker, parole chiave, one-shot per tipo."""
from __future__ import annotations

import pytest

from sc_hooks import core
from sc_hooks.handlers import thinking_nudge as tn


def _ctx(subagent, prompt, sid="s1"):
    return core.Ctx(event="PreToolUse", payload={}, session_id=sid, tool="Agent",
                    tool_input={"subagent_type": subagent, "prompt": prompt}, cwd="C:/x", role="meta", agent="meta")


@pytest.mark.parametrize("subagent,prompt,expected", [
    ("w-debugger", "Fix the crash in parser.py", "single-file debug"),
    ("w-debugger", "There is a Race Condition across modules", "multi-file race / cross-file"),
    ("w-planner", "Plan the ARCHITECTURE migration", "architectural / multi-phase planning"),
    ("w-planner", "Plan the rename", "single-phase planning"),
    ("w-reviewer", "Do a scathingly-deep pass", "scathingly-deep / architectural review"),
    ("w-reviewer", "Quick look at the diff", ""),
    ("w-doc", "Check coherence of chapters", "cross-section / coherence doc work"),
    ("w-implementer", "Change 12 files in the API", "multi-file / architectural implementation"),
    ("w-implementer", "Change 3 files in the API", ""),
    ("w-merger", "Resolve a semantic conflict", "semantic merge conflict"),
    ("w-explorer", "architecture race condition", ""),
    ("w-debugger", "Think hard about this crash", ""),
    ("w-planner", "ULTRATHINK the architecture", ""),
    ("w-debugger", "", ""),
])
def test_task_class(subagent, prompt, expected):
    assert tn.task_class(subagent, prompt) == expected


def test_one_shot_per_worker_type(home):
    res = tn.handle(_ctx("w-debugger", "Fix the crash"))
    assert "w-debugger on single-file debug" in res.context[0] and "effort:" in res.context[0]
    assert tn.handle(_ctx("w-debugger", "Fix another crash")) is None
    assert tn.handle(_ctx("w-planner", "Plan the rename")) is not None     # altro tipo: nuova nota
    assert tn.handle(_ctx("w-debugger", "Fix the crash", sid="s2")) is not None


def test_no_marker_consumed_without_nudge(home):
    assert tn.handle(_ctx("w-reviewer", "Quick look")) is None
    assert tn.handle(_ctx("w-reviewer", "deep review please")) is not None


@pytest.mark.parametrize("ti", [{}, {"subagent_type": None, "prompt": "x"}, {"subagent_type": "w-debugger"},
                                {"subagent_type": ["w-debugger"], "prompt": {"a": 1}},
                                {"subagent_type": "w-debugger", "prompt": 42}])
def test_bad_input(home, ti):
    ctx = _ctx("x", "y")
    ctx.tool_input = ti
    assert tn.handle(ctx) is None
