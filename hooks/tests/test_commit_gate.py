"""Test della guardia commit-gate: segreti in stage, oggetto del commit, add massivo, push."""
from __future__ import annotations

import shutil
import subprocess

import pytest
from conftest import msys, win

from sc_hooks.handlers import commit_gate

SHELLS = ("Bash", "PowerShell")
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"                       # spezzata: niente segreti interi nel sorgente
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
ASSIGN = "API_KEY" + ' = "' + "a1B2c3D4e5F6g7H8i9J0" + '"'


def denied(res):
    return res is not None and bool(res.block)


def notes(res):
    return "\n".join(res.context) if res else ""


@pytest.fixture
def no_git(monkeypatch):
    """Sostituisce la chiamata a git: i test sull'oggetto non devono dipendere da un repo."""
    seen = []

    def fake(repo, include_worktree=False):
        seen.append((repo, include_worktree))
        return []
    monkeypatch.setattr(commit_gate, "staged_additions", fake)
    return seen


# ---------------------------------------------------------------- oggetto del commit
@pytest.mark.parametrize("cmd", [
    "git commit -m 'feat: add x'", 'git commit -m "fix(core): y"', "git commit -m 'feat!: breaking'",
    "git commit -am 'chore: tidy'", "git commit --message='docs: z'", "git commit --message \"test: t\"",
    "git commit -m \"$(cat <<'EOF'\nfeat: heredoc subject\n\nbody; with | separators\nEOF\n)\"",
    "git commit -m @'\nfix: here-string subject\n\nbody\n'@", "git commit -m 'refactor: x'",
    "git commit --amend --no-edit", "git commit --amend", "git commit", "git commit -F msg.txt",
    "git -C /c/repo commit -m 'perf: faster'", "& git.exe commit -m 'build: b'",
])
@pytest.mark.parametrize("tool", SHELLS)
def test_conventional_or_absent_subject_is_silent(mk, no_git, tool, cmd):
    assert commit_gate.handle(mk(tool, command=cmd)) is None


@pytest.mark.parametrize("cmd", [
    "git commit -m 'added stuff'", 'git commit -m "Fix: capitalised type"', "git commit -am wip",
    "git commit -m \"$(cat <<'EOF'\nupdate things\nEOF\n)\"", "git commit -m @\"\nupdate things\n\"@",
    "git commit --amend -m 'oops'", "cd r && git add a.py && git commit -m 'feat:nospace'",
])
@pytest.mark.parametrize("tool", SHELLS)
def test_non_conventional_subject_warns_but_does_not_block(mk, no_git, tool, cmd):
    res = commit_gate.handle(mk(tool, command=cmd))
    assert res is not None and not res.block and "conventional" in notes(res)


# ---------------------------------------------------------------- add massivo e push
@pytest.mark.parametrize("cmd", ["git add -A", "git add .", "git add --all", "git add -u", "git -C /c/r add .",
                                 "git.exe add -A; git status", "cd r && git add . && git commit -m 'feat: x'"])
@pytest.mark.parametrize("tool", SHELLS)
def test_bulk_add_warns(mk, no_git, tool, cmd):
    res = commit_gate.handle(mk(tool, command=cmd))
    assert res is not None and not res.block and "bulk" in notes(res)


@pytest.mark.parametrize("cmd", ["git add src/a.py tests/test_a.py", "git add ./a.py", "git add -p a.py", "git add -- a.py"])
def test_explicit_add_is_silent(mk, no_git, cmd):
    assert commit_gate.handle(mk("Bash", command=cmd)) is None


@pytest.mark.parametrize("cmd", ["git push", "git push origin main", "git -C /c/r push", "& git push", "git status; git push"])
@pytest.mark.parametrize("tool", SHELLS)
def test_push_reminder_is_one_line(mk, no_git, tool, cmd):
    res = commit_gate.handle(mk(tool, command=cmd))
    assert res is not None and not res.block and len(res.context) == 1 and "\n" not in res.context[0]
    assert "push" in res.context[0].lower()


@pytest.mark.parametrize("cmd", ["echo 'git push'", "git status", "ls", "git log --grep push", "", "gitk"])
def test_unrelated_commands_silent(mk, no_git, cmd):
    assert commit_gate.handle(mk("Bash", command=cmd)) is None and no_git == []


# ---------------------------------------------------------------- segreti (git simulato)
@pytest.mark.parametrize("line", [AWS, f"key = '{AWS}'", PEM, ASSIGN, "export GITHUB_TOKEN=ghp_abcdefghij0123456789",
                                  "DB_PASSWORD='Zm9vYmFyYmF6cXV4MTIzNA=='", "MY_SECRET_2=abcdefabcdefabcdefabcdef"])
def test_secret_shapes_block(mk, monkeypatch, line):
    monkeypatch.setattr(commit_gate, "staged_additions", lambda repo, include_worktree=False: [("conf/app.env", line)])
    res = commit_gate.handle(mk("Bash", command="git commit -m 'feat: x'"))
    assert denied(res) and "conf/app.env" in res.block and line not in res.block


@pytest.mark.parametrize("line", ["MAX_TOKEN_COUNT = 100", 'API_KEY_HEADER = "X-Api-Key"', "SESSION_TOKEN_TTL=3600",
                                  "password = input()", "token = get_token()", "AKIA-short", "# BEGIN PRIVATE notes"])
def test_everyday_constants_pass(mk, monkeypatch, line):
    monkeypatch.setattr(commit_gate, "staged_additions", lambda repo, include_worktree=False: [("a.py", line)])
    assert commit_gate.handle(mk("Bash", command="git commit -m 'feat: x'")) is None


def test_git_failure_is_fail_open(mk, monkeypatch):
    monkeypatch.setattr(commit_gate, "staged_additions", lambda repo, include_worktree=False: None)
    assert commit_gate.handle(mk("Bash", command="git commit -m 'feat: x'")) is None


def test_repo_resolution(mk, home, no_git):
    proj = home / "code" / "proj"
    commit_gate.handle(mk("Bash", command="git commit -m 'feat: x'", cwd=win(proj)))
    commit_gate.handle(mk("Bash", command=f"git -C {msys(home / 'code' / 'other')} commit -m 'feat: x'"))
    commit_gate.handle(mk("PowerShell", command=f"git -C \"{win(home / 'code' / 'other')}\" commit -am 'feat: x'"))
    commit_gate.handle(mk("Bash", command="cd sub && git commit -m 'feat: x'", cwd=msys(proj)))
    base = str(home).replace("\\", "/").lower()
    assert no_git == [(f"{base}/code/proj", False), (f"{base}/code/other", False), (f"{base}/code/other", True),
                      (f"{base}/code/proj/sub", False)]


# ---------------------------------------------------------------- segreti (git vero)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git non disponibile")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, timeout=30)


@pytest.fixture
def repo(home):
    r = home / "code" / "realrepo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "a.py")
    _git(r, "commit", "-q", "-m", "chore: init")
    return r


@needs_git
@pytest.mark.parametrize("form", [win, msys])
def test_real_repo_staged_secret_blocks(mk, repo, form):
    (repo / "conf.py").write_text(f"{ASSIGN}\n", encoding="utf-8")
    _git(repo, "add", "conf.py")
    res = commit_gate.handle(mk("Bash", command=f"git -C {form(repo)} commit -m 'feat: conf'", cwd=str(repo.parent)))
    assert denied(res) and "conf.py" in res.block
    assert denied(commit_gate.handle(mk("PowerShell", command="git commit -m 'feat: conf'", cwd=win(repo))))


@needs_git
def test_real_repo_clean_and_removed_secret_pass(mk, repo):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    assert commit_gate.handle(mk("Bash", command="git commit -m 'fix: x'", cwd=win(repo))) is None
    # un segreto NON in stage non blocca `git commit`, ma blocca `git commit -a`
    (repo / "a.py").write_text(f"x = 2\n{ASSIGN}\n", encoding="utf-8")
    assert commit_gate.handle(mk("Bash", command="git commit -m 'fix: x'", cwd=win(repo))) is None
    assert denied(commit_gate.handle(mk("Bash", command="git commit -am 'fix: x'", cwd=win(repo))))


@needs_git
def test_real_not_a_repo_is_fail_open(mk, home):
    assert commit_gate.handle(mk("Bash", command="git commit -m 'feat: x'", cwd=win(home))) is None
    assert commit_gate.handle(mk("Bash", command="git -C /c/does/not/exist commit -m 'feat: x'")) is None


def test_timeout_is_fail_open(monkeypatch):
    def slow(*a, **k):
        assert k["timeout"] == commit_gate.GIT_TIMEOUT_S == 5
        raise subprocess.TimeoutExpired("git", 5)
    monkeypatch.setattr(subprocess, "run", slow)
    assert commit_gate.staged_additions("c:/x") is None


def test_git_missing_is_fail_open(monkeypatch):
    def gone(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", gone)
    assert commit_gate.staged_additions("c:/x") is None


@pytest.mark.parametrize("ti", [{}, {"command": None}, {"command": 7}, {"command": ["git", "commit"]}])
def test_malformed_input(mk, no_git, ti):
    assert commit_gate.handle(mk("Bash", tool_input=ti)) is None
    assert commit_gate.handle(mk("PowerShell", tool_input=None)) is None
