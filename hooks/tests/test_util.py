"""Test delle utilità condivise sui comandi shell (`handlers/_util.py`)."""
from __future__ import annotations

import pytest

from sc_hooks.handlers import _util


def verbs(cmd, cwd="C:/x"):
    return [c.verb for c in _util.git_calls(cmd, cwd)]


@pytest.mark.parametrize("cmd", [
    "git commit -m 'feat: x'",
    "  git   commit",
    "git.exe commit -m x",
    "& git commit -m x",
    '& "C:\\Program Files\\Git\\cmd\\git.exe" commit -m x',
    "/usr/bin/git commit",
    "/mingw64/bin/git.exe commit",
    "git -C /c/repo commit -m x",
    'git -C "C:\\my repo" commit -m x',
    "git -c user.name=x --no-pager commit",
    "git --git-dir=/r/.git --work-tree=/r commit",
    "FOO=1 BAR=2 git commit",
    "cd repo && git commit -m x",
    "cd repo; git commit -m x",
    "git add a.py || git commit",
    "echo hi | git commit -F -",
    "git status\ngit commit -m x",
    "git \\\ncommit -m x",
    "git `\ncommit -m x",
    "if true; then git commit -m x; fi",
    'bash -c "git commit -m x"',
    "powershell -Command \"git commit -m x\"",
    "sudo git commit",
])
def test_git_commit_detected(cmd):
    assert "commit" in verbs(cmd)


@pytest.mark.parametrize("cmd", [
    "echo 'please run git commit later'",
    'echo "a; git commit"',
    "Write-Output \"x | git commit\"",
    "gitk commit",
    "legit commit",
    "git --version",
    "git log --grep commit",
    "grep -r 'git commit' .",
    "",
    "   ",
])
def test_git_commit_not_detected(cmd):
    assert "commit" not in verbs(cmd)


def test_unbalanced_quotes_fall_back_to_naive_split():
    cmd = "cat <<EOF > notes.txt\ndon't panic\nEOF\ngit push origin main"
    assert "push" in verbs(cmd)


def test_redirect_operators_do_not_split():
    assert _util.segments("cmd 2>&1 | tee log") == ["cmd 2>&1", "tee log"]
    assert _util.segments("echo x >| f && echo y &> g") == ["echo x >| f", "echo y &> g"]


def test_c_dir_and_cwd_tracking():
    (call,) = _util.git_calls("cd sub && git -C ../other add a.py", "C:/x/proj")
    assert call.c_dir == "../other" and call.cwd == "c:/x/proj/sub" and call.repo == "c:/x/proj/other"
    (call,) = _util.git_calls('Set-Location "C:\\my repo"; git status', "C:/x")
    assert call.repo == "c:/my repo"
    (call,) = _util.git_calls("git -C /c/Users/x/repo status", "C:/x")
    assert call.repo == "c:/users/x/repo"


def test_gh_calls():
    assert _util.gh_calls("gh pr merge 12 --squash") == [["pr", "merge", "12", "--squash"]]
    assert _util.gh_calls("cd r && gh.exe release create v1") == [["release", "create", "v1"]]
    assert _util.gh_calls("echo gh pr merge") == []


@pytest.mark.parametrize("bad", [None, 42, [], {}, b"git commit"])
def test_non_string_commands(bad):
    assert _util.segments(bad) == [] and _util.git_calls(bad) == [] and _util.gh_calls(bad) == []


def test_huge_command_is_bounded():
    assert _util.git_calls("x" * 500_000 + "; git commit") == []


def test_canon_handles_bad_types():
    assert _util.canon(None) == "" and _util.canon(42) == "" and _util.canon("a.py", cwd=7).endswith("/a.py")
