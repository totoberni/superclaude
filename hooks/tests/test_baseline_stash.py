"""Test di baseline-stash: policy no-commit, one-shot, forme dei percorsi, git assente."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from conftest import msys, win
from sc_hooks import core
from sc_hooks.handlers import baseline_stash as bs

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git non disponibile")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *args], check=True, capture_output=True, timeout=30)


@pytest.fixture
def repo(home, monkeypatch):
    """Repo git sporco `~/code/secretproj` con un file tracciato modificato e uno nuovo."""
    monkeypatch.setattr(bs, "CONFIG_FILE", home / ".claude" / "hooks" / "no-commit-projects.local")
    monkeypatch.delenv("CLAUDE_COMMIT_POLICY", raising=False)
    r = home / "code" / "SecretProj"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    (r / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    (r / "new.txt").write_text("n\n", encoding="utf-8")
    return r


def _list(home, *names):
    cfg = home / ".claude" / "hooks" / "no-commit-projects.local"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("# comment\n\n" + "\n".join(names) + "\n", encoding="utf-8")


def _ctx(home, file_path, sid="s1", cwd=None, tool="Edit"):
    return core.Ctx(event="PreToolUse", payload={}, session_id=sid, tool=tool, tool_input={"file_path": file_path},
                    cwd=str(home / "code") if cwd is None else cwd, role="plain", agent="")


@needs_git
def test_stashes_for_listed_project_via_file_path(home, repo):
    _list(home, "other", "secretproj")
    res = bs.handle(_ctx(home, win(repo / "src" / "a.py")))
    status = home / ".claude" / "tmp" / "s1-baseline.txt"
    diff = home / ".claude" / "tmp" / "s1-baseline.diff"
    assert "src/a.py" in status.read_text() and "new.txt" in status.read_text()
    assert "+x = 2" in diff.read_text()
    text = res.context[0]
    assert status.as_posix() in text and diff.as_posix() in text and "\\" not in text
    assert bs.handle(_ctx(home, win(repo / "src" / "a.py"))) is None       # una volta per sessione


@needs_git
def test_msys_path_and_new_subdir(home, repo):
    _list(home, "SecretProj")
    res = bs.handle(_ctx(home, msys(repo / "brand" / "new" / "dir" / "f.py"), tool="Write"))
    assert res is not None and (home / ".claude" / "tmp" / "s1-baseline.diff").exists()


@needs_git
def test_relative_path_and_cwd_match(home, repo):
    _list(home, "secretproj")
    assert bs.handle(_ctx(home, "src/a.py", cwd=str(repo))) is not None


@needs_git
def test_unlisted_write_does_not_consume_marker(home, repo):
    _list(home, "secretproj")
    assert bs.handle(_ctx(home, win(home / "code" / "proj" / "x.py"))) is None
    assert not (home / ".claude" / "tmp").exists()
    assert bs.handle(_ctx(home, win(repo / "src" / "a.py"))) is not None


@needs_git
def test_env_policy_without_list(home, repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_COMMIT_POLICY", "false")
    assert bs.handle(_ctx(home, win(repo / "new.txt"))) is not None


def test_no_config_no_env_is_noop(home, repo):
    assert bs.handle(_ctx(home, win(repo / "src" / "a.py"))) is None
    assert not (home / ".claude" / "tmp").exists()


def test_not_a_repo_and_git_failures(home, monkeypatch):
    monkeypatch.setattr(bs, "CONFIG_FILE", home / ".claude" / "hooks" / "no-commit-projects.local")
    _list(home, "proj")
    assert bs.handle(_ctx(home, win(home / "code" / "proj" / "x.py"))) is None

    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert bs.handle(_ctx(home, win(home / "code" / "proj" / "x.py"), sid="s2")) is None


def test_git_timeout_is_swallowed(home, monkeypatch):
    monkeypatch.setattr(bs, "CONFIG_FILE", home / ".claude" / "hooks" / "no-commit-projects.local")
    _list(home, "proj")
    seen = {}

    def slow(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(subprocess, "run", slow)
    assert bs.handle(_ctx(home, win(home / "code" / "proj" / "x.py"))) is None
    assert seen["timeout"] == 5


@pytest.mark.parametrize("ti", [{}, {"file_path": None}, {"file_path": 12}, {"file_path": ["a"]}])
def test_bad_input(home, monkeypatch, ti):
    monkeypatch.setattr(bs, "CONFIG_FILE", home / ".claude" / "hooks" / "no-commit-projects.local")
    _list(home, "nothing-matches")
    ctx = _ctx(home, "x")
    ctx.tool_input = ti
    assert bs.handle(ctx) is None
