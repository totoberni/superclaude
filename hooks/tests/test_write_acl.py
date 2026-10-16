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


# --------------------------------------------------------------------------
# Worker di sola lettura: Write solo per checkpoint e scratch
# --------------------------------------------------------------------------
READONLY = sorted(write_acl._READONLY_WRITERS)


@pytest.fixture
def scratch(home, monkeypatch):
    """Radice scratch finta %LOCALAPPDATA%/Temp/claude dentro la HOME finta."""
    local = home / "AppData" / "Local"
    (local / "Temp" / "claude").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("TEMP", str(local / "Temp"))
    return local / "Temp" / "claude"


def fwd(p) -> str:
    """Forma Windows con slash in avanti: C:/Users/..."""
    return str(p).replace("\\", "/")


def lower_drive(p) -> str:
    s = win(p)
    return s[0].lower() + s[1:]


def upper(p) -> str:
    return win(p).upper()


FORMS = [win, msys, fwd, lower_drive, upper]


def target(home, scratch, base, tail):
    roots = {"home": home, "claude": home / ".claude", "scratch": scratch, "temp": scratch.parent}
    return roots[base] / tail


def ro(mk, agent="w-reviewer", tool="Write", **ti):
    return write_acl.handle(mk(tool, role="worker", agent=agent, **ti))


ALLOWED = [
    ("claude", "plans/camp/checkpoints/W-1.md"),
    ("claude", "plans/camp/checkpoints/sub/deep/r.json"),
    ("claude", "plans/checkpoints/checkpoints/W.md"),
    ("claude", "tmp/x.txt"),
    ("claude", "tmp/wft-2/hooks/sc_hooks/a.py"),
    ("claude", "tmp/a/../b.txt"),
    ("scratch", "C--Users-x/sess/scratchpad/out.txt"),
    ("scratch", "s/a/../b.txt"),
]


@pytest.mark.parametrize("agent", READONLY)
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("base,tail", ALLOWED)
def test_readonly_writer_allowed_roots(mk, home, scratch, agent, form, base, tail):
    path = form(target(home, scratch, base, tail))
    assert ro(mk, agent, file_path=path, content="x") is None


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"),
                                      ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_readonly_writer_every_write_tool(mk, home, scratch, tool, key):
    assert ro(mk, "w-tester", tool, **{key: win(home / ".claude" / "tmp" / "n.ipynb")}) is None
    assert denied(ro(mk, "w-tester", tool, **{key: win(home / "code" / "proj" / "n.ipynb")}))


def test_readonly_writer_home_and_relative_forms(mk, home, scratch):
    assert ro(mk, file_path="~/.claude/tmp/x.txt") is None
    assert ro(mk, file_path=r"~\.claude\plans\c\checkpoints\W.md") is None
    assert ro(mk, file_path="$HOME/.claude/tmp/x.txt") is None
    assert ro(mk, file_path="out.txt", cwd=msys(scratch / "s")) is None
    assert ro(mk, file_path=r"sub\out.txt", cwd=win(home / ".claude" / "tmp")) is None
    assert ro(mk, file_path=win(home / ".claude") + "\\\\tmp\\\\x.txt\\") is None    # separatori doppi e finali
    assert denied(ro(mk, file_path="../../code/proj/a.py", cwd=win(home / ".claude" / "tmp")))
    assert denied(ro(mk, file_path="a.py"))                     # cwd = progetto
    assert denied(ro(mk, file_path="~/code/proj/a.py"))


DENIED = [
    ("home", "code/proj/src/a.py"),
    ("home", "code/proj/docs/report.md"),
    ("home", "elsewhere/x.txt"),
    ("claude", "skills/x/SKILL.md"),
    ("claude", "plans/camp/notes.md"),
    ("claude", "plans/camp/state.md"),
    ("claude", "agents/_ephemeral/w-tmp.md"),
    ("claude", "config/other"),
    ("temp", "other.txt"),
]


@pytest.mark.parametrize("agent", READONLY)
@pytest.mark.parametrize("form", [win, msys, fwd])
@pytest.mark.parametrize("base,tail", DENIED)
def test_readonly_writer_denied_elsewhere(mk, home, scratch, agent, form, base, tail):
    res = ro(mk, agent, file_path=form(target(home, scratch, base, tail)), content="x")
    assert denied(res) and "checkpoint" in res.block


TRICKS = [
    ("claude", "tmp-evil/x"),
    ("claude", "tmpx"),
    ("claude", "tmp"),
    ("claude", "tmp/../skills/x.md"),
    ("claude", "plans/a/checkpoints-x/f.md"),
    ("claude", "plans/a/checkpointsx"),
    ("claude", "plans/a/checkpoints"),
    ("claude", "plans/checkpoints/f.md"),
    ("claude", "plans/a/b/checkpoints/f.md"),
    ("claude", "plans/a/checkpoints/../notes.md"),
    ("claude", "plans/a/checkpoints/../../../skills/x"),
    ("claude", "plans/a/checkpoints/../../../rules/x"),
    ("claude", "xplans/a/checkpoints/f.md"),
    ("home", ".claude-evil/tmp/x"),
    ("temp", "claude-evil/x"),
    ("temp", "claudex"),
    ("scratch", "../evil.txt"),
    ("scratch", "x/../../evil.txt"),
    ("home", "AppData/Local/Temp2/claude/x"),
]


@pytest.mark.parametrize("form", [win, msys, fwd, lower_drive])
@pytest.mark.parametrize("base,tail", TRICKS)
def test_readonly_writer_prefix_tricks_denied(mk, home, scratch, form, base, tail):
    assert denied(ro(mk, "w-design-reviewer", file_path=form(target(home, scratch, base, tail))))


def test_readonly_writer_root_itself_denied(mk, home, scratch):
    for root in (home / ".claude" / "tmp", scratch, home / ".claude" / "plans" / "c" / "checkpoints"):
        for raw in (win(root) + "\\", msys(root) + "/", fwd(root) + "//"):
            assert denied(ro(mk, file_path=raw))


def test_readonly_writer_tree_denials_still_apply_first(mk, home, scratch):
    res = ro(mk, file_path=win(home / ".claude" / "hooks" / "x.py"))
    assert denied(res) and "read-only for workers" in res.block
    res = ro(mk, file_path=win(home / ".claude" / "plans" / "a" / "checkpoints" / ".." / ".." / ".." / "rules" / "x"))
    assert denied(res) and "~/.claude/rules/ is read-only for workers" in res.block
    res = ro(mk, file_path=win(home / ".claude" / "plans" / "c" / "checkpoints" / "plan.md"))
    assert denied(res) and "meta-only" in res.block
    res = ro(mk, file_path=win(home / ".claude" / "settings.json"))
    assert denied(res) and "owner-run-only" in res.block
    res = ro(mk, file_path=win(home / "code" / "proj" / "CLAUDE.md"))
    assert denied(res) and res.block.startswith("project-local CLAUDE.md")


def test_readonly_deny_message_names_roots(mk, home, scratch):
    res = ro(mk, "w-hostile-reviewer", file_path=win(home / "code" / "proj" / "a.py"))
    assert res.block.startswith("w-hostile-reviewer is read-only")
    for root in ("~/.claude/plans/<campaign>/checkpoints/", "~/.claude/tmp/", "%LOCALAPPDATA%/Temp/claude/"):
        assert root in res.block
    assert "agent=w-hostile-reviewer" in res.block and res.block.isascii()


def test_scratch_root_falls_back_to_temp(mk, home, scratch, monkeypatch):
    path = win(scratch / "a.txt")
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setenv("TEMP", win(scratch.parent) + "\\")
    assert ro(mk, file_path=path) is None
    monkeypatch.delenv("TEMP")
    assert denied(ro(mk, file_path=path))
    assert ro(mk, file_path=win(home / ".claude" / "tmp" / "a.txt")) is None


def test_scratch_root_env_trailing_slash_and_short_name(mk, home, scratch, monkeypatch):
    path = win(scratch / "a.txt")
    monkeypatch.setenv("LOCALAPPDATA", win(scratch.parent.parent) + "\\")
    assert ro(mk, file_path=path) is None
    # LOCALAPPDATA in forma 8.3: la radice si espande come i percorsi in ingresso
    short = "C:\\Users\\FAKEUS~1"
    monkeypatch.setenv("LOCALAPPDATA", short + "\\AppData\\Local")
    real_realpath = write_acl._util.os.path.realpath

    def fake_realpath(p):
        s = str(p).replace("\\", "/")
        return fwd(home) + s[len(short):] if s.lower().startswith(fwd(short).lower()) else real_realpath(p)
    monkeypatch.setattr(write_acl._util.os.path, "realpath", fake_realpath)
    assert ro(mk, file_path=path) is None
    assert denied(ro(mk, file_path=win(home / "code" / "proj" / "a.py")))


@pytest.mark.parametrize("role,agent", [("worker", "w-implementer"), ("worker", "w-doc"), ("worker", "w-coder"),
                                        ("meta", "meta"), ("plain", "")])
def test_other_identities_unchanged(mk, home, scratch, role, agent):
    for p in (home / "code" / "proj" / "src" / "a.py", home / "elsewhere" / "x.txt",
              home / ".claude" / "skills" / "x" / "SKILL.md", home / ".claude" / "plans" / "c" / "notes.md",
              home / ".claude" / "tmp" / "x.txt", scratch / "x.txt"):
        assert write_acl.handle(mk("Write", role=role, agent=agent, file_path=win(p))) is None
    hooks = write_acl.handle(mk("Write", role=role, agent=agent, file_path=win(home / ".claude" / "hooks" / "x.py")))
    assert denied(hooks) if role == "worker" else hooks is None


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Bash", "PowerShell", "WebFetch"])
def test_readonly_writer_non_write_tools_not_checked(mk, home, scratch, monkeypatch, capsys, tool):
    from sc_hooks import core
    monkeypatch.setattr(core, "_REGISTRY", [r for r in core._REGISTRY if r[2] == write_acl.NAME])
    monkeypatch.delenv("SUPERCLAUDE_GUARDS", raising=False)
    monkeypatch.delenv("SUPERCLAUDE_GUARD_WRITE_ACL", raising=False)
    src = win(home / "code" / "proj" / "src" / "a.py")
    assert core.dispatch(mk(tool, role="worker", agent="w-reviewer", file_path=src, path=src, command="cat a.py")) == 0
    assert "GUARD-BLOCK" not in capsys.readouterr().err
    # controllo: lo stesso bersaglio con Write passa dal dispatch e viene bloccato
    assert core.dispatch(mk("Write", role="worker", agent="w-reviewer", file_path=src)) == 2
    assert "GUARD-BLOCK [write-acl] w-reviewer is read-only" in capsys.readouterr().err
