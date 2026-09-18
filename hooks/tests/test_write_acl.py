"""Test della guardia write-acl."""
from __future__ import annotations

import pytest
from conftest import msys, win

from sc_hooks.handlers import write_acl

ROLES = ("plain", "meta", "worker")


def denied(res):
    return res is not None and bool(res.block)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("form", [win, msys, str])
@pytest.mark.parametrize("tail", [
    ".claude/settings.json", ".claude/agents/x.md", "sub/.claude/a/b.md",
    "CLAUDE.md", "sub/CLAUDE.md", "CLAUDE.local.md", "claude.MD",
])
def test_project_local_claude_denied_for_every_role(mk, home, role, form, tail):
    path = form(home / "code" / "proj" / tail)
    assert denied(write_acl.handle(mk("Write", role=role, file_path=path, content="x")))


def test_project_local_relative_and_home_forms(mk, home):
    assert denied(write_acl.handle(mk("Edit", file_path=".claude/settings.json")))
    assert denied(write_acl.handle(mk("Edit", file_path="../other/CLAUDE.md")))
    assert denied(write_acl.handle(mk("Edit", file_path="~/code/proj/CLAUDE.md")))
    assert denied(write_acl.handle(mk("MultiEdit", file_path="$HOME/code/proj/.claude/x")))
    assert denied(write_acl.handle(mk("Write", file_path="x/../.claude/y", cwd=msys(home / "code" / "proj"))))


def test_notebook_path_field_is_checked(mk, home):
    assert denied(write_acl.handle(mk("NotebookEdit", notebook_path=win(home / "code" / "p" / ".claude" / "n.ipynb"))))


@pytest.mark.parametrize("role", ROLES)
def test_ordinary_project_and_outside_files_allowed(mk, home, role):
    for p in (home / "code" / "proj" / "src" / "a.py", home / "code" / "proj" / "docs" / "claude.md.txt",
              home / "code" / "proj" / "my.claude" / "a", home / "elsewhere" / "CLAUDE.md",
              home / "elsewhere" / ".claude" / "x"):
        assert write_acl.handle(mk("Write", role=role, file_path=win(p))) is None


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json", "SETTINGS.JSON"])
@pytest.mark.parametrize("form", [win, msys])
def test_settings_owner_only(mk, home, name, form):
    path = form(home / ".claude" / name)
    assert denied(write_acl.handle(mk("Edit", role="meta", file_path=path)))
    assert denied(write_acl.handle(mk("Edit", role="worker", file_path=path)))
    assert write_acl.handle(mk("Edit", role="plain", file_path=path)) is None


def test_settings_tilde_form(mk):
    assert denied(write_acl.handle(mk("Write", role="meta", file_path="~/.claude/settings.json")))
    assert denied(write_acl.handle(mk("Write", role="worker", file_path=r"~\.claude\settings.local.json")))


WORKER_ONLY = [
    "plans/p1/plan.md", "plans/p1/context.md", "plans/a/b/plan.md", "plans/plan.md",
    "config/git-policy", "hooks/dispatch.py", "hooks/sc_hooks/core.py", "rules/00-x.md",
    "agents/w-coder.md", "agents/_ephemeralX/a.md",
]


@pytest.mark.parametrize("tail", WORKER_ONLY)
@pytest.mark.parametrize("form", [win, msys])
def test_worker_only_denials(mk, home, tail, form):
    path = form(home / ".claude" / tail)
    assert denied(write_acl.handle(mk("Write", role="worker", file_path=path)))
    assert write_acl.handle(mk("Write", role="meta", file_path=path)) is None
    assert write_acl.handle(mk("Write", role="plain", file_path=path)) is None


@pytest.mark.parametrize("tail", [
    "agents/_ephemeral/w-tmp.md", "agents/_pending_promotion/w-new.md", "plans/p1/notes.md",
    "plans/p1/report-w-coder.md", "tmp/x.txt", "skills/git/SKILL.md", "config/other",
])
def test_worker_allowed_paths(mk, home, tail):
    assert write_acl.handle(mk("Write", role="worker", file_path=win(home / ".claude" / tail))) is None


def test_worker_relative_path_from_claude_home(mk, home):
    ctx = mk("Edit", role="worker", file_path=r"rules\05-x.md", cwd=win(home / ".claude"))
    assert denied(write_acl.handle(ctx))


def test_short_8dot3_name_is_expanded(mk, home, monkeypatch):
    real = str(home / ".claude" / "settings.json")
    monkeypatch.setattr(write_acl._util.os.path, "realpath", lambda p: real)
    assert denied(write_acl.handle(mk("Write", role="meta", file_path=r"C:\Users\FAKEUS~1\.claude\settings.json")))


@pytest.mark.parametrize("ti", [{}, {"file_path": None}, {"file_path": 42}, {"file_path": ["a"]}, {"file_path": "   "},
                                {"file_path": {"x": 1}}])
def test_malformed_input_passes(mk, ti):
    assert write_acl.handle(mk("Write", role="worker", tool_input=ti)) is None


def test_non_dict_tool_input_and_cwd(mk):
    assert write_acl.handle(mk("Write", role="worker", tool_input="garbage")) is None
    assert write_acl.handle(mk("Write", role="worker", cwd=None, file_path="a.py")) is None


def test_registered_name():
    from sc_hooks import core
    assert any(n == "write-acl" and e == "PreToolUse" and set(t) == set(core.WRITE_TOOLS) for e, t, n, _ in core._REGISTRY)
