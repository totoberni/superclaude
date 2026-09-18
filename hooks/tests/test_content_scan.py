"""Test della guardia content-scan. I trattini vietati sono costruiti con chr()."""
from __future__ import annotations

import pytest
from conftest import msys, win

from sc_hooks.handlers import content_scan

EM, EN = chr(0x2014), chr(0x2013)


def denied(res):
    return res is not None and bool(res.block)


@pytest.mark.parametrize("dash", [EM, EN])
def test_dash_blocked_in_every_write_shape(mk, home, dash):
    p = win(home / "notes.md")
    text = f"alpha {dash} beta"
    assert denied(content_scan.handle(mk("Write", file_path=p, content=text)))
    assert denied(content_scan.handle(mk("Edit", file_path=p, old_string="a", new_string=text)))
    assert denied(content_scan.handle(mk("MultiEdit", file_path=p, edits=[{"new_string": "ok"}, {"new_string": text}])))
    assert denied(content_scan.handle(mk("NotebookEdit", notebook_path=p, new_source=text)))
    assert denied(content_scan.handle(mk("Agent", prompt=text)))


def test_dash_only_in_old_string_is_fine(mk, home):
    ctx = mk("Edit", file_path=win(home / "a.md"), old_string=f"x {EM} y", new_string="x; y")
    assert content_scan.handle(ctx) is None


def test_hyphen_and_plain_text_pass(mk, home):
    assert content_scan.handle(mk("Write", file_path=win(home / "a.md"), content="a - b -- c")) is None
    assert content_scan.handle(mk("Agent", prompt="review src/a.py")) is None


@pytest.mark.parametrize("form", [win, msys, str])
def test_write_over_existing_file_scans_only_added_lines(mk, home, form):
    f = home / "code" / "proj" / "old.md"
    f.write_text(f"title\nold {EM} line\n", encoding="utf-8")
    keep = f"title\nold {EM} line\nnew clean line\n"
    assert content_scan.handle(mk("Write", file_path=form(f), content=keep)) is None
    moved = f"old {EM} line\ntitle\n"
    assert content_scan.handle(mk("Write", file_path=form(f), content=moved)) is None
    added = f"title\nold {EM} line\nanother {EN} one\n"
    assert denied(content_scan.handle(mk("Write", file_path=form(f), content=added)))
    edited = f"title\nold {EM} line changed\n"
    assert denied(content_scan.handle(mk("Write", file_path=form(f), content=edited)))


def test_write_relative_path_resolves_against_cwd(mk, home):
    f = home / "code" / "proj" / "old.md"
    f.write_text(f"old {EM} line\n", encoding="utf-8")
    assert content_scan.handle(mk("Write", file_path="old.md", content=f"old {EM} line\nok\n")) is None
    assert denied(content_scan.handle(mk("Write", file_path="new.md", content=f"old {EM} line\n")))


def test_edit_never_gets_the_grandfather_diff(mk, home):
    f = home / "code" / "proj" / "old.md"
    f.write_text(f"old {EM} line\n", encoding="utf-8")
    assert denied(content_scan.handle(mk("Edit", file_path=win(f), new_string=f"old {EM} line")))


FORBIDDEN = [
    "see ~/.claude/rules/00.md", r"see ~\.claude\hooks", "in .claude/rules we say", "the agent-memory dir",
    "shared/projects/foo.md", "class/meta", "per MEMORY.md", "mtm.md", "LTM.md", ".memory.db",
    "memory_db.py query", "Meta says so", "memory.md says", "According to the gotchas file",
    "see the project memory", "fixes M-12", "per GM-3 and G-27", "cf. MT-4", "(CW-9)", "W-2 applies",
]


@pytest.mark.parametrize("snippet", FORBIDDEN)
@pytest.mark.parametrize("form", [win, msys])
def test_firewall_blocks_internal_refs_in_project_files(mk, home, snippet, form):
    ctx = mk("Write", file_path=form(home / "code" / "proj" / "README.md"), content=f"intro\n{snippet}\n")
    res = content_scan.handle(ctx)
    assert denied(res) and "firewall" in res.block


def test_firewall_absolute_claude_home(mk, home):
    target = win(home / "code" / "proj" / "run.ps1")
    assert denied(content_scan.handle(mk("Write", file_path=target, content=f"cat {win(home / '.claude' / 'x.md')}")))
    assert denied(content_scan.handle(mk("Write", file_path=target, content=f"cat {msys(home / '.claude' / 'x.md')}")))


@pytest.mark.parametrize("role", ["plain", "meta", "worker"])
def test_firewall_applies_to_every_role_and_relative_paths(mk, role):
    assert denied(content_scan.handle(mk("Edit", role=role, file_path="docs/a.md", new_string="agent-memory")))


def test_firewall_scope(mk, home):
    text = "see ~/.claude/rules and M-12"
    assert content_scan.handle(mk("Write", file_path=win(home / ".claude" / "rules" / "x.md"), content=text)) is None
    assert content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / "x.md"), content=text)) is None
    assert content_scan.handle(mk("Agent", prompt=text)) is None


@pytest.mark.parametrize("text", [
    "removed subsystems: .comms.db .broker.db comms_db.py", "section C4 and B-tree", "HTTP-200 and X-1",
    "project-local .claude/settings.json docs", "the summary.md file",
])
def test_firewall_clean_text_passes(mk, home, text):
    assert content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "a.md"), content=text)) is None


@pytest.mark.parametrize("ti", [
    {}, {"content": None}, {"content": 42, "file_path": 7}, {"edits": 5}, {"edits": [None, 3, {"new_string": None}]},
    {"prompt": ["x"]}, {"file_path": None, "content": "fine"},
])
@pytest.mark.parametrize("tool", ["Write", "MultiEdit", "Agent"])
def test_malformed_input_never_raises(mk, tool, ti):
    assert content_scan.handle(mk(tool, tool_input=ti)) is None


def test_non_dict_tool_input(mk):
    assert content_scan.handle(mk("Write", tool_input=None)) is None


def test_source_files_contain_no_forbidden_dash():
    from pathlib import Path
    root = Path(content_scan.__file__).resolve().parent
    mine = ["_util", "write_acl", "content_scan", "git_policy", "commit_gate", "git_verb",
            "worker_verify", "notebook_guard"]
    for name in mine:
        text = (root / f"{name}.py").read_text(encoding="utf-8")
        assert EM not in text and EN not in text, name
