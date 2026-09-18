"""Test della guardia worker-verify: promemoria R-3 come additionalContext."""
from __future__ import annotations

import json

import pytest

from sc_hooks import core
from sc_hooks.handlers import worker_verify


def test_note_names_the_worker_and_fires_once(mk):
    res = worker_verify.handle(mk("Agent", event="PostToolUse", subagent_type="w-coder", prompt="do x"))
    assert res is not None and not res.block
    assert "w-coder" in res.context[0] and "git diff --stat" in res.context[0] and "R-3" in res.context[0]
    assert worker_verify.handle(mk("Agent", event="PostToolUse", subagent_type="w-tester")) is None


def test_new_session_fires_again(mk):
    assert worker_verify.handle(mk("Agent", event="PostToolUse", subagent_type="a")) is not None
    other = mk("Agent", event="PostToolUse", subagent_type="a")
    other.session_id = "s2"
    assert worker_verify.handle(other) is not None


@pytest.mark.parametrize("ti", [{}, {"subagent_type": None}, {"subagent_type": 5}, {"subagent_type": "  "}, None, "x"])
def test_missing_type_still_reminds_with_generic_name(mk, ti):
    res = worker_verify.handle(mk("Agent", event="PostToolUse", tool_input=ti))
    assert res is not None and "'subagent'" in res.context[0]


@pytest.mark.parametrize("role", ["plain", "meta", "worker"])
def test_every_role(mk, role):
    assert worker_verify.handle(mk("Agent", role=role, event="PostToolUse", subagent_type="w-x")) is not None


def test_reaches_the_model_as_additional_context(mk, monkeypatch, capsys):
    monkeypatch.setattr(core, "_REGISTRY", [("PostToolUse", ("Agent",), "worker-verify", worker_verify.handle)])
    assert core.dispatch(mk("Agent", event="PostToolUse", subagent_type="w-coder")) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert "R-3" in out["hookSpecificOutput"]["additionalContext"] and captured.err == ""


def test_registration():
    regs = [(e, t) for e, t, n, _ in core._REGISTRY if n == "worker-verify"]
    assert regs == [("PostToolUse", ("Agent",))]
