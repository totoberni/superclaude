"""Test della guardia git-verb: solo avvisi, mai blocchi."""
from __future__ import annotations

import pytest

from sc_hooks.handlers import git_verb

SHELLS = ("Bash", "PowerShell")


def text(res):
    assert res is None or not res.block          # questa guardia non blocca mai
    return "\n".join(res.context) if res else ""


@pytest.mark.parametrize("cmd", [
    "git -C /c/Users/x/repo add /c/Users/x/repo/src/a.py",
    r"git -C C:\Users\x\repo add C:\Users\x\repo\src\a.py",
    r"git -C C:\Users\x\repo add /c/Users/x/repo/src/a.py",
    'git -C "C:/Users/x/my repo" diff -- "C:/Users/x/my repo/a.py"',
    "git -C repo add repo/src/a.py",
    r"git.exe -C .\repo add .\repo\a.py",
    "cd /c/Users/x && git -C ~/code/r log -- ~/code/r/a.py",
])
@pytest.mark.parametrize("tool", SHELLS)
def test_repeated_dash_c_dir_warns(mk, tool, cmd):
    assert "repeats that directory" in text(git_verb.handle(mk(tool, command=cmd)))


@pytest.mark.parametrize("cmd", [
    "git -C /c/Users/x/repo add src/a.py", "git -C repo status", "git -C /c/Users/x/repo add /c/Users/x/repository/a.py",
    "git -C . add ./a.py", "git add /c/Users/x/repo/a.py", "git -C repo log --format=repo/%h",
])
def test_clean_dash_c_usage_is_silent(mk, cmd):
    assert git_verb.handle(mk("Bash", command=cmd)) is None


@pytest.mark.parametrize("cmd,needle", [
    ("git checkout -b feature/x", "checkout -b"), ("git checkout -B feature/x", "checkout -b"),
    ("git switch -c feature/x", "switch -c"), ("git switch --create feature/x", "switch -c"),
    ("git branch feature/x", "git branch <name>"), ("git -C /c/r branch hotfix main", "git branch <name>"),
    ("git merge feature/x", "git merge"), ("git fetch && git merge --no-ff origin/main", "git merge"),
    ("& git.exe checkout -b x", "checkout -b"), ("git status; git switch -c y", "switch -c"),
    ("git fetch `\n; git merge main", "git merge"),
])
@pytest.mark.parametrize("tool", SHELLS)
def test_branch_operations_warn(mk, tool, cmd, needle):
    assert needle in text(git_verb.handle(mk(tool, command=cmd)))


@pytest.mark.parametrize("cmd", [
    "git checkout main", "git checkout -- a.py", "git switch main", "git branch", "git branch -a", "git branch -d old",
    "git branch --show-current", "git merge --abort", "git merge-base a b", "git log --merges",
    "echo 'git merge x'", "git status", "ls -la", "",
])
@pytest.mark.parametrize("tool", SHELLS)
def test_harmless_commands_silent(mk, tool, cmd):
    assert git_verb.handle(mk(tool, command=cmd)) is None


@pytest.mark.parametrize("role", ["plain", "meta", "worker"])
def test_every_role_and_combined_notes(mk, role):
    out = text(git_verb.handle(mk("Bash", role=role, command="git -C r checkout -b x && git -C r merge r/y")))
    assert "checkout -b" in out and "git merge" in out and "repeats" in out


@pytest.mark.parametrize("ti", [{}, {"command": None}, {"command": 3.5}, {"command": {"a": 1}}])
def test_malformed_input(mk, ti):
    assert git_verb.handle(mk("Bash", tool_input=ti)) is None
    assert git_verb.handle(mk("PowerShell", tool_input=[])) is None
