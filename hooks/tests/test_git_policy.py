"""Test della guardia git-policy: flag `/git true|false` e auto-sblocco dei worker."""
from __future__ import annotations

import pytest
from conftest import msys, win

from sc_hooks.handlers import git_policy

SHELLS = ("Bash", "PowerShell")
ROLES = ("plain", "meta", "worker")


def denied(res):
    return res is not None and bool(res.block)


@pytest.fixture
def flag(home):
    path = home / ".claude" / "config" / "git-policy"

    def _set(word):
        path.write_text(word + "\n", encoding="utf-8")
        return path
    return _set


BLOCKED = [
    "git commit -m 'feat: x'", "git push", "git push origin main --force", "git rebase main", "git merge feature",
    "git cherry-pick abc123", "git revert HEAD", "git reset --hard HEAD~1", "git reset HEAD~1 --hard",
    "git tag v1.0", "git tag -a v1 -m rel", "git tag -d v1", "git am x.patch", "git commit-tree abc",
    "gh pr merge 12 --squash", "gh release create v1.0",
    "git -C /c/repo commit -m x", "git.exe push", "& git commit -m x", "cd r && git add a && git commit -m x",
    "git status; git push", "git status | Out-Null; git push", "git add a.py\ngit commit -m x",
    "git `\n  commit -m x", "git \\\n  push", 'bash -c "git push"', "pwsh -Command 'git commit -m x'",
    '& "C:\\Program Files\\Git\\cmd\\git.exe" push', "FOO=1 git commit",
]

ALLOWED = [
    "git status", "git log --oneline -5", "git diff --cached", "git add a.py", "git fetch", "git pull --ff-only",
    "git reset HEAD a.py", "git reset --soft HEAD~1", "git tag", "git tag -l 'v*'", "git tag --list",
    "git tag --contains abc", "git merge --abort", "git rebase --abort", "git stash", "git checkout main",
    "git show HEAD", "git branch -a", "gh pr view 12", "gh pr list", "gh release list", "gh pr create --fill",
    "echo 'git commit'", 'Write-Output "then run git push"', "git log --grep 'merge'", "ls", "",
]


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocked_while_disabled(mk, flag, tool, cmd):
    flag("disabled")
    res = git_policy.handle(mk(tool, command=cmd))
    assert denied(res) and "/git true" in res.block


@pytest.mark.parametrize("role", ROLES)
def test_blocked_for_every_role(mk, flag, role):
    flag("disabled")
    assert denied(git_policy.handle(mk("Bash", role=role, command="git commit -m x")))


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", ALLOWED)
def test_read_only_git_allowed_while_disabled(mk, flag, tool, cmd):
    flag("disabled")
    assert git_policy.handle(mk(tool, command=cmd)) is None


@pytest.mark.parametrize("word", ["false", "FALSE", "off", "0", "Disabled", "\ufeffdisabled"])
def test_disabled_spellings(mk, flag, word):
    flag(word)
    assert denied(git_policy.handle(mk("Bash", command="git push")))


@pytest.mark.parametrize("word", ["enabled", "true", "", "garbage", "disabled maybe"])
def test_enabled_or_unknown_content_passes(mk, flag, word):
    flag(word)
    assert git_policy.handle(mk("Bash", command="git commit -m x && git push")) is None


def test_missing_or_unreadable_flag_means_enabled(mk, home):
    assert git_policy.handle(mk("Bash", command="git push")) is None
    (home / ".claude" / "config" / "git-policy").mkdir()          # una directory: la lettura fallisce
    assert git_policy.handle(mk("Bash", command="git push")) is None


def test_env_override_of_flag_path(mk, home, tmp_path, monkeypatch):
    other = tmp_path / "policy-flag"
    other.write_text("disabled", encoding="utf-8")
    monkeypatch.setenv("SUPERCLAUDE_GIT_POLICY_FILE", str(other))
    assert denied(git_policy.handle(mk("Bash", command="git push")))
    assert denied(git_policy.handle(mk("Bash", role="worker", command=f"echo enabled > {msys(other)}")))


def _flag_writes(home):
    w, m = win(home / ".claude" / "config" / "git-policy"), msys(home / ".claude" / "config" / "git-policy")
    bash = [
        f"echo enabled > {m}", f"echo enabled >> {m}", f"echo enabled >| {m}", f"echo enabled >{m}",
        "echo enabled > ~/.claude/config/git-policy", 'echo enabled > "$HOME/.claude/config/git-policy"',
        "echo enabled > ${HOME}/.claude/config/git-policy", "echo enabled > ~/.claude//config/./git-policy",
        "cd ~/.claude/config && echo enabled > git-policy", "cd ~/.claude && printf enabled > config/git-policy",
        f"echo enabled | tee {m}", f"cp /tmp/x {m}", f"mv /tmp/x '{m}'", f"rm {m}", f"sed -i s/dis/en/ {m}",
        f"dd if=/tmp/x of={m}", f"echo enabled \\\n > {m}", f"true && echo enabled > {m}; ls",
        f'bash -c "echo enabled > {m}"',
    ]
    ps = [
        f'Set-Content -Path "{w}" -Value enabled', f"Set-Content {w} enabled", f"'enabled' | Out-File {w}",
        f"'enabled' | Out-File -FilePath:{w}", f"'enabled' > {w}", f"Add-Content {w} enabled",
        f"Copy-Item C:\\tmp\\x {w}", f"Move-Item C:\\tmp\\x -Destination {w} -Force", f"Remove-Item {w}",
        "Set-Content $env:USERPROFILE\\.claude\\config\\git-policy enabled",
        "Set-Content $HOME\\.claude\\config\\git-policy enabled", "sc ~\\.claude\\config\\git-policy enabled",
        "Set-Location ~\\.claude\\config; 'enabled' | Out-File git-policy", f"'enabled' `\n | Out-File {w}",
        f"& Set-Content {w} enabled",
    ]
    return bash, ps


def test_worker_flag_write_blocked_in_any_policy_state(mk, home, flag):
    bash, ps = _flag_writes(home)
    for state in ("enabled", "disabled", None):
        if state:
            flag(state)
        for tool, cmds in (("Bash", bash), ("PowerShell", ps)):
            for cmd in cmds:
                res = git_policy.handle(mk(tool, role="worker", command=cmd))
                assert denied(res) and "flag" in res.block, (tool, cmd)


@pytest.mark.parametrize("role", ["meta", "plain"])
def test_flag_write_allowed_for_meta_and_plain(mk, home, role):
    bash, ps = _flag_writes(home)
    for tool, cmds in (("Bash", bash), ("PowerShell", ps)):
        for cmd in cmds:
            assert git_policy.handle(mk(tool, role=role, command=cmd)) is None, cmd


def test_worker_may_read_the_flag(mk, home):
    m, w = msys(home / ".claude" / "config" / "git-policy"), win(home / ".claude" / "config" / "git-policy")
    for tool, cmd in (("Bash", f"cat {m}"), ("Bash", f"cat {m} 2>&1"), ("Bash", f"cat {m} > /tmp/copy"),
                      ("Bash", f"grep -c disabled {m} 2>/dev/null"), ("PowerShell", f"Get-Content {w}"),
                      ("PowerShell", f"Get-Content {w} | Out-File C:\\tmp\\copy.txt"),
                      ("Bash", "echo x > ~/.claude/config/git-policy.bak"), ("Bash", "echo git-policy")):
        assert git_policy.handle(mk(tool, role="worker", command=cmd)) is None, cmd


@pytest.mark.parametrize("ti", [{}, {"command": None}, {"command": 42}, {"command": ["git", "push"]}, {"command": "  "}])
def test_malformed_input(mk, flag, ti):
    flag("disabled")
    assert git_policy.handle(mk("Bash", role="worker", tool_input=ti)) is None


def test_non_dict_tool_input_and_bad_cwd(mk, flag):
    flag("disabled")
    assert git_policy.handle(mk("Bash", tool_input="git push")) is None
    ctx = mk("Bash", role="worker", command="git push")
    ctx.cwd = None
    assert denied(git_policy.handle(ctx))
