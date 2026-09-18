"""Test di context-check: soglia, one-shot, file mancante o sporco."""
from __future__ import annotations

import json
import time

import pytest

from sc_hooks import core
from sc_hooks.handlers import context_check as cc


@pytest.fixture
def ctxfile(home, monkeypatch):
    path = home / ".claude" / ".context-latest.json"
    monkeypatch.setattr(cc, "CONTEXT_FILE", path)
    return path


def _ctx(sid="s1", role="plain"):
    return core.Ctx(event="PreToolUse", payload={}, session_id=sid, tool="Read", tool_input={}, cwd="C:/x",
                    role=role, agent="")


def _write(path, **data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_file_is_silent(ctxfile):
    assert cc.handle(_ctx()) is None


def test_below_threshold(ctxfile):
    _write(ctxfile, used_tokens=146999, window_size=200000, used_pct=73.5)
    assert cc.handle(_ctx()) is None


def test_warns_once_per_session_for_any_role(ctxfile):
    _write(ctxfile, used_tokens=147000, window_size=200000, used_pct=73.5, ts=int(time.time()))
    res = cc.handle(_ctx())
    assert "147000/200000" in res.context[0] and "auto-compact near 167000" in res.context[0]
    assert "(74%)" in res.context[0] or "(73%)" in res.context[0]
    assert cc.handle(_ctx()) is None                          # one-shot
    assert cc.handle(_ctx(sid="s2", role="worker")) is not None


def test_stale_sample_ignored(ctxfile):
    _write(ctxfile, used_tokens=190000, window_size=200000, ts=int(time.time()) - 7200)
    assert cc.handle(_ctx()) is None


@pytest.mark.parametrize("raw", ["", "not json", "[1,2]", '{"used_tokens": "abc", "window_size": 200000}',
                                 '{"used_tokens": true, "window_size": true}', '{"used_tokens": -5, "window_size": 0}',
                                 '{"used_tokens": 190000}'])
def test_garbage_is_silent(ctxfile, raw):
    ctxfile.write_text(raw, encoding="utf-8")
    assert cc.handle(_ctx()) is None


def test_missing_pct_and_string_numbers(ctxfile):
    _write(ctxfile, used_tokens="190000", window_size=200000.0, used_pct=None)
    assert "(?%)" in cc.handle(_ctx()).context[0]
