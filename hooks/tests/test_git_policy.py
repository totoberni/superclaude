"""Test della guardia git-policy: flag `/git true|false`, auto-sblocco dei worker, HUMAN GATE di `mem sync`.

Anche la regola pura lib/sc_gitflag.py, condivisa con il motore di sync.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import msys, win

from sc_hooks import core
from sc_hooks.handlers import git_policy

import sc_gitflag  # noqa: E402 - core (via conftest) ha messo <code home>/lib in sys.path

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


def _stream_writes(home):
    w, c = win(home / ".claude" / "config" / "git-policy"), win(home / ".claude" / "config")
    m = msys(home / ".claude" / "config" / "git-policy")
    return [
        ("PowerShell", f"Set-Content -LiteralPath '{w}::$DATA' -Value enabled"),
        ("PowerShell", f"cmd /c 'echo enabled> {w}::$DATA'"),
        ("PowerShell", f"Set-Content -LiteralPath '{c}::$INDEX_ALLOCATION\\git-policy' -Value enabled"),
        ("PowerShell", f"Set-Content -LiteralPath '{c}:$I30:$INDEX_ALLOCATION\\git-policy' -Value enabled"),
        ("Bash", f"echo enabled > '{m}::$DATA'"),
        ("Bash", f'echo enabled > "{m}::\\$DATA"'),
        ("PowerShell", f'Set-Content -LiteralPath "{w}::`$DATA" -Value enabled'),
    ]


def test_worker_flag_write_through_stream_designator_blocked(mk, home):
    for tool, cmd in _stream_writes(home):
        res = git_policy.handle(mk(tool, role="worker", command=cmd))
        assert denied(res) and "flag" in res.block, (tool, cmd)


@pytest.mark.parametrize("role", ["meta", "plain"])
def test_flag_write_through_stream_designator_allowed_for_meta_and_plain(mk, home, role):
    for tool, cmd in _stream_writes(home):
        assert git_policy.handle(mk(tool, role=role, command=cmd)) is None, (tool, cmd)


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


# Profilo non default (deepseek): git sempre spento salvo flag esplicito, override ignorato.

@pytest.fixture
def deepseek(monkeypatch):
    monkeypatch.setenv("SC_PROFILE", "deepseek")


DEEPSEEK_CMDS = ["git commit -m x", "git push", "git -C /c/repo commit -m x", "git -C /c/repo push"]


@pytest.mark.parametrize("cmd", DEEPSEEK_CMDS)
def test_deepseek_without_flag_denies(mk, home, deepseek, cmd):
    assert denied(git_policy.handle(mk("Bash", command=cmd)))


def test_deepseek_unreadable_flag_denies(mk, home, deepseek):
    (home / ".claude" / "config" / "git-policy").mkdir()
    assert denied(git_policy.handle(mk("Bash", command="git push")))


@pytest.mark.parametrize("cmd", DEEPSEEK_CMDS)
def test_deepseek_flag_false_override_true_still_denies(mk, flag, deepseek, tmp_path, monkeypatch, cmd):
    flag("false")
    other = tmp_path / "override-flag"
    other.write_text("enabled", encoding="utf-8")
    monkeypatch.setenv("SUPERCLAUDE_GIT_POLICY_FILE", str(other))
    assert denied(git_policy.handle(mk("Bash", command=cmd)))


@pytest.mark.parametrize("cmd", DEEPSEEK_CMDS)
def test_deepseek_flag_true_allows(mk, flag, deepseek, cmd):
    flag("true")
    assert git_policy.handle(mk("Bash", command=cmd)) is None


def test_default_profile_missing_flag_allows_and_override_honoured(mk, home, tmp_path, monkeypatch):
    assert git_policy.handle(mk("Bash", command="git push")) is None
    other = tmp_path / "override-flag"
    other.write_text("disabled", encoding="utf-8")
    monkeypatch.setenv("SUPERCLAUDE_GIT_POLICY_FILE", str(other))
    assert denied(git_policy.handle(mk("Bash", command="git -C /c/repo push")))


# --------------------------------------------------------------------------
# sc_gitflag: la regola pura, senza ambiente
# --------------------------------------------------------------------------
MISSING, UNREADABLE, NUL_PATH = object(), object(), object()


def _policy_path(tmp_path, content):
    path = tmp_path / "git-policy"
    if content is UNREADABLE:
        path.mkdir()                       # una directory: la lettura fallisce
    elif content is NUL_PATH:
        return str(tmp_path / "bad\0name")  # ValueError all'apertura
    elif content is not MISSING:
        path.write_text(content, encoding="utf-8")
    return path


TRUTH_TABLE = [
    # (contenuto del flag, fail_closed, git_disabled atteso)
    ("disabled", False, True), ("disabled", True, True),
    ("enabled", False, False), ("enabled", True, False),
    ("garbage", False, False), ("garbage", True, True),
    ("", False, False), ("", True, True),
    (MISSING, False, False), (MISSING, True, True),
    (UNREADABLE, False, False), (UNREADABLE, True, True),
    (NUL_PATH, False, False), (NUL_PATH, True, True),
]


@pytest.mark.parametrize("content,fail_closed,expected", TRUTH_TABLE)
def test_gitflag_truth_table(tmp_path, content, fail_closed, expected):
    assert sc_gitflag.git_disabled(_policy_path(tmp_path, content), fail_closed) is expected


@pytest.mark.parametrize("fail_closed", [False, True])
@pytest.mark.parametrize("word", ["false", "FALSE", "off", "0", "no", "Disabled", "\ufeffdisabled", "  disabled \n"])
def test_gitflag_disabled_spellings(tmp_path, word, fail_closed):
    assert sc_gitflag.git_disabled(_policy_path(tmp_path, word), fail_closed) is True


@pytest.mark.parametrize("fail_closed", [False, True])
@pytest.mark.parametrize("word", ["true", "TRUE", "on", "1", "yes", "Enabled", "\ufeffenabled", " enabled\r\n"])
def test_gitflag_enabled_spellings(tmp_path, word, fail_closed):
    assert sc_gitflag.git_disabled(_policy_path(tmp_path, word), fail_closed) is False


def test_gitflag_accepts_str_and_path(tmp_path):
    path = _policy_path(tmp_path, "disabled")
    assert sc_gitflag.git_disabled(str(path), False) is True
    assert sc_gitflag.git_disabled(path, False) is True


def test_gitflag_ignores_env_override(tmp_path, monkeypatch):
    flag = _policy_path(tmp_path, "enabled")
    override = tmp_path / "override-flag"
    override.write_text("disabled", encoding="utf-8")
    monkeypatch.setenv("SUPERCLAUDE_GIT_POLICY_FILE", str(override))
    for profile in ("superclaude", "deepseek"):
        monkeypatch.setenv("SC_PROFILE", profile)
        assert sc_gitflag.git_disabled(flag, False) is False
        assert sc_gitflag.git_disabled(flag, True) is False
    # Un override abilitante non salva un flag assente sotto fail_closed
    override.write_text("enabled", encoding="utf-8")
    assert sc_gitflag.git_disabled(tmp_path / "missing", True) is True


def test_gitflag_source_never_reads_the_environment():
    text = Path(sc_gitflag.__file__).read_text(encoding="utf-8")
    for token in ("environ", "getenv", "import os"):
        assert token not in text, token
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_gitflag_imports_under_python_dash_s():
    lib = Path(sc_gitflag.__file__).resolve().parent
    code = f"import sys; sys.path.insert(0, {str(lib)!r}); import sc_gitflag; print(sc_gitflag.__file__)"
    out = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).resolve() == Path(sc_gitflag.__file__).resolve()


# --------------------------------------------------------------------------
# La guardia delega a sc_gitflag e risolve da se' l'override
# --------------------------------------------------------------------------
def test_hook_uses_the_lib_module():
    # Risolto dalla <code home>/lib che core mette in sys.path, senza un secondo inserimento
    assert git_policy._GITFLAG is sc_gitflag
    assert Path(sc_gitflag.__file__).resolve().parent == Path(core._LIB_DIR).resolve()


def test_hook_passes_resolved_path_and_profile_fail_mode(home, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sc_gitflag, "git_disabled", lambda path, fail_closed: seen.append((Path(path), fail_closed)))
    git_policy.git_disabled()
    override = tmp_path / "override-flag"
    monkeypatch.setenv("SUPERCLAUDE_GIT_POLICY_FILE", str(override))
    git_policy.git_disabled()
    monkeypatch.setenv("SC_PROFILE", "deepseek")
    git_policy.git_disabled()
    flag = home / ".claude" / "config" / "git-policy"
    assert seen == [(flag, False), (override, False), (flag, True)]


def test_import_failure_is_logged_and_yields_none(home, monkeypatch):
    monkeypatch.setitem(sys.modules, "sc_gitflag", None)          # import sc_gitflag -> ImportError
    assert git_policy._import_gitflag() is None
    assert "import/sc_gitflag" in (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")


def test_broken_lib_is_caught_not_only_import_error(home, tmp_path, monkeypatch):
    broken = tmp_path / "brokenlib"
    broken.mkdir()
    (broken / "sc_gitflag.py").write_text("raise RuntimeError('lib rotta')\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "sc_gitflag")
    monkeypatch.syspath_prepend(str(broken))
    assert git_policy._import_gitflag() is None
    assert "lib rotta" in (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")


def _lib_on_path(tmp_path, monkeypatch, source):
    # Una sc_gitflag.py in testa a sys.path, al posto di quella gia' importata
    other = tmp_path / "otherlib"
    other.mkdir()
    (other / "sc_gitflag.py").write_text(source, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "sc_gitflag")
    monkeypatch.syspath_prepend(str(other))


def test_lib_exiting_at_import_is_caught(home, tmp_path, monkeypatch):
    # SystemExit non e' un Exception: uscito dalla guardia chiuderebbe il dispatcher con via libera
    _lib_on_path(tmp_path, monkeypatch, "raise SystemExit(0)\n")
    assert git_policy._import_gitflag() is None
    assert "import/sc_gitflag: SystemExit(0)" in (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")


def test_copy_outside_lib_dir_is_refused(home, tmp_path, monkeypatch):
    # Una copia che lascia passare git, trovata prima di lib (es. in PYTHONPATH), non deve vincere
    _lib_on_path(tmp_path, monkeypatch, "def git_disabled(policy_path, fail_closed):\n    return False\n")
    assert git_policy._import_gitflag() is None
    assert "shadowed" in (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")


@pytest.fixture
def no_gitflag(monkeypatch):
    monkeypatch.setattr(git_policy, "_GITFLAG", None)


def _lib_failure_denial(res):
    return (denied(res) and "sc_gitflag" in res.block and "sc_profile" in res.block and "hooks.log" in res.block
            and "/git true" not in res.block)


@pytest.mark.parametrize("state", [None, "enabled", "disabled"])
def test_without_gitflag_default_profile_fails_closed(mk, flag, no_gitflag, state):
    # Una lib rotta non e' uno stato del flag: git resta bloccato e il messaggio dice perche'
    if state:
        flag(state)
    assert git_policy.git_disabled() is True
    for cmd in ("git push", "git commit -m x", "gh pr merge 12"):
        assert _lib_failure_denial(git_policy.handle(mk("Bash", command=cmd))), cmd
    assert git_policy.handle(mk("Bash", command="git status")) is None


def test_without_gitflag_deepseek_fails_closed(mk, flag, deepseek, no_gitflag):
    flag("true")
    assert _lib_failure_denial(git_policy.handle(mk("Bash", command="git push")))


def test_without_gitflag_other_rules_stay_active(mk, home, no_gitflag):
    m = msys(home / ".claude" / "config" / "git-policy")
    assert denied(git_policy.handle(mk("Bash", role="worker", command=f"echo enabled > {m}")))
    assert denied(git_policy.handle(mk("Bash", command="mem sync --take-over")))


# Difetti della lib alla chiamata, non all'import (review T1.4b N2)
def _raises(path, fail_closed):
    raise RuntimeError("lib rotta alla chiamata")


def _exits(path, fail_closed):
    raise SystemExit("flag unreadable")


LIB_CALL_DEFECTS = {
    "raises": lambda mp: mp.setattr(sc_gitflag, "git_disabled", _raises),
    "exits": lambda mp: mp.setattr(sc_gitflag, "git_disabled", _exits),
    "removed": lambda mp: mp.delattr(sc_gitflag, "git_disabled"),
    "wrong_signature": lambda mp: mp.setattr(sc_gitflag, "git_disabled", lambda path, *, profile: False),
    "returns_zero": lambda mp: mp.setattr(sc_gitflag, "git_disabled", lambda path, fail_closed: 0),
    "returns_none": lambda mp: mp.setattr(sc_gitflag, "git_disabled", lambda path, fail_closed: None),
    "returns_str": lambda mp: mp.setattr(sc_gitflag, "git_disabled", lambda path, fail_closed: "enabled"),
}


@pytest.mark.parametrize("profile", ["superclaude", "deepseek"])
@pytest.mark.parametrize("state", [None, "enabled", "disabled"])
@pytest.mark.parametrize("defect", sorted(LIB_CALL_DEFECTS))
def test_lib_call_time_defect_fails_closed(mk, home, flag, monkeypatch, profile, state, defect):
    monkeypatch.setenv("SC_PROFILE", profile)
    if state:
        flag(state)
    LIB_CALL_DEFECTS[defect](monkeypatch)
    assert git_policy.git_disabled() is True
    for role in ROLES:
        for cmd in ("git push", "git commit -m x", "gh pr merge 12"):
            assert _lib_failure_denial(git_policy.handle(mk("Bash", role=role, command=cmd))), (role, cmd)
        assert git_policy.handle(mk("Bash", role=role, command="git status")) is None
    # Ogni difetto, anche un risultato non bool, lascia la sua riga: il diniego rimanda a hooks.log
    assert "call/sc_gitflag" in (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")


def _profile_defect():
    raise AttributeError("module 'sc_profile' has no attribute 'GIT_POLICY_REL'")


@pytest.mark.parametrize("profile", ["superclaude", "deepseek"])
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("broken", ["policy_file", "_non_default_profile"])
def test_profile_lib_defect_fails_closed_for_every_role(mk, home, flag, monkeypatch, profile, role, broken):
    # Per un worker policy_file() serve gia' all'auto-sblocco: l'eccezione non deve arrivare al
    # fail-open di core, ne' annullare /git false o l'HUMAN GATE; nel log la colpa va a sc_profile
    monkeypatch.setenv("SC_PROFILE", profile)
    flag("enabled")
    monkeypatch.setattr(git_policy, broken, _profile_defect)
    for cmd in ("git push", "git commit -m x", "gh pr merge 12"):
        assert _lib_failure_denial(git_policy.handle(mk("Bash", role=role, command=cmd))), cmd
    res = git_policy.handle(mk("Bash", role=role, command="mem sync --take-over"))
    assert denied(res) and "HUMAN GATE" in res.block
    assert git_policy.handle(mk("Bash", role=role, command="git status")) is None
    log = (home / ".claude" / "logs" / "hooks.log").read_text(encoding="utf-8")
    assert "call/policy_file" in log and "call/sc_gitflag" not in log


@pytest.mark.parametrize("profile", ["superclaude", "deepseek"])
def test_good_lib_passes_git_push_when_enabled(mk, flag, monkeypatch, profile):
    monkeypatch.setenv("SC_PROFILE", profile)
    flag("enabled")
    assert git_policy.handle(mk("Bash", command="git push")) is None
    flag("disabled")
    res = git_policy.handle(mk("Bash", command="git push"))
    assert denied(res) and "/git false" in res.block and "sc_gitflag" not in res.block


# --------------------------------------------------------------------------
# HUMAN GATE: `mem sync --take-over | --allow-mass-delete` mai da un agente
# --------------------------------------------------------------------------
OWNER_FLAG_CMDS = [
    "mem sync --take-over", "mem sync --allow-mass-delete", "mem sync --take-over --allow-mass-delete",
    "mem --db C:/x/m.db sync --take-over", "mem sync --TAKE-OVER", "MEM SYNC --take-over", "mem sync push --take-over",
    "mem sync --take-over=1", "mem sync --allow-mass-delete=yes", 'mem sync "--take-over"',
    "mem sync '--allow-mass-delete'", "mem.cmd sync --take-over", "& mem.ps1 sync --allow-mass-delete",
    '& "$env:USERPROFILE\\.claude\\bin\\mem.ps1" sync --take-over',
    "~/.claude/.venv/Scripts/python.exe ~/.claude/scripts/memory/memory_db.py sync --take-over",
    "scpy memory/memory_db.py sync --allow-mass-delete", "cd ~/.claude && mem sync --take-over",
    "git status; mem sync --take-over", "FOO=1 mem sync --take-over", "mem sync --take-over\r\n",
    'bash -c "mem sync --take-over"', "pwsh -Command 'mem sync --allow-mass-delete'", "sh -c 'mem sync --take-over'",
    'cmd /c "mem sync --take-over"', "cmd.exe /c mem sync --take-over", 'iex "mem sync --take-over"',
    "Invoke-Expression 'mem sync --allow-mass-delete'", 'powershell -NoProfile -Command "mem sync --take-over"',
    "wsl -e mem sync --take-over", "python -c \"import subprocess; subprocess.run(['mem','sync','--take-over'])\"",
    "mem sync `\n  --take-over", "mem sync \\\n  --allow-mass-delete", "mem sync --take`-over",
    'mem sync --take"-"over', "mem sync --take\\-over", "mem s'y'nc --take-over", "echo $(mem sync --take-over)",
    # Continuazione di riga dentro la parola: bash toglie `\` + a capo e la ricuce
    "mem sync --take\\\n-over", "mem sync --allow-mass\\\r\n-delete", "mem s\\\nync --take-over",
    # Operatori di shell attaccati all'opzione
    "mem sync --take-over;ls", "mem sync --take-over|Out-Null", "mem sync --take-over&", "mem sync --take-over>o.txt",
    "mem sync --take-over<i.txt",
]

# Lanciatori: la stringa citata e' la riga di comando del processo lanciato (review T1.4b #3)
OWNER_FLAG_LAUNCHERS = [
    'Start-Process pwsh -ArgumentList "-NoExit -Command mem sync --take-over"',
    'Start-Process wt -ArgumentList "pwsh -NoExit -Command mem sync --take-over"',
    "Start-Process pwsh -ArgumentList '-NoExit','-Command','mem sync --take-over'",
    'Start-Process mem -ArgumentList "sync --take-over"', "Start-Process mem -ArgumentList sync,--take-over",
    "Start-Process mem -ArgumentList @('sync','--allow-mass-delete')",
    "Start-Process mem -ArgumentList:'sync --take-over'", "Start-Process mem -ArgumentList:sync,--take-over",
    "saps mem 'sync --take-over'",
    'cmd /c start pwsh -NoExit -Command "mem sync --take-over"', "mem sync $'--take-over'",
    "echo --take-over | xargs mem sync", "& mem sync @('--take-over')", "F=--take-over; mem sync $F",
    "$f = '--allow-mass-delete'; mem sync $f",
    # PowerShell spezza `"x"--take-over` e `'--take-over'x` in due argomenti, il secondo e' l'opzione
    'mem sync "x"--take-over', "mem sync '--take-over'x", 'mem sync "q"--take"-"over',
    # Espansioni e caret con l'opzione scritta per intero (review T1.4b N1); `--take$()-over`
    # vale per bash e PowerShell, coperti entrambi da SHELLS
    "mem sync ${F:---take-over}", "mem sync ${F---take-over}", "F=1; mem sync ${F:+--take-over}",
    "mem sync -$'-take-over'", "mem sync --take-over$''", "mem sync --take$()-over", "mem sync --take-over$null",
    "cmd /c mem sync --take^-over", "mem.cmd sync --take^-over",
    # Virgolette tipografiche (da U+2018 a U+201E): PowerShell 7 e 5.1 le trattano come virgolette
    "mem \u201csync\u201d --take-over", "mem sync \u2018--allow-mass-delete\u2019",
    "Start-Process mem -ArgumentList \u2018sync\u2019,\u2018--take-over\u2019",
    "mem \u201bsync\u201b --take-over", "mem sync \u201e--take-over\u201c", "mem sync \u201a--allow-mass-delete\u2018",
]

# Testo che nomina soltanto l'opzione accanto a `sync`: falso positivo accettato, lato sicuro
OWNER_FLAG_MENTIONS = [
    'echo "mem sync --take-over"', 'git commit -m "docs: never run mem sync --take-over"',
    "mem search 'sync --allow-mass-delete'", 'Write-Output "then run mem sync --allow-mass-delete"',
]

OWNER_FLAG_ALLOWED = [
    "mem sync", "mem sync status", "mem sync drill", "mem sync init --label win11 --new-key", "mem sync --",
    "mem sync --allow-other", "grep -n -- --take-over memory_db.py", "grep -rn -- --allow-mass-delete scripts",
    "rg -n allow-mass-delete scripts", "pytest hooks/tests -k take_over", "memsync --take-over",
    # Niente abbreviazioni: il parser di `mem sync` usa allow_abbrev=False (review T1.4b #2)
    "mem sync --take", "mem sync --t", "mem sync --allow", "mem sync --allow-mass", "mem sync --all",
    "mem sync --take-ove", "mem sync status --all", "echo sync --all", "mutagen sync terminate --all",
    "uv sync --all-extras", "git fetch --all; git submodule sync", "git submodule sync --recursive",
    # Token piu' lungo o dentro un percorso: argparse non lo riconosce come l'opzione
    "mem sync --take-overs", "mem sync --no-take-over", "mem sync --take-over-notes.md",
    "cat docs/--take-over-notes.md && mem sync status", "mem sync ./--take-over",
    "mem sync status > logs/--allow-mass-delete.txt", "mem \u201csync\u201d status",
]


@pytest.mark.parametrize("tool", SHELLS + ("Monitor",))
@pytest.mark.parametrize("cmd", OWNER_FLAG_CMDS + OWNER_FLAG_LAUNCHERS + OWNER_FLAG_MENTIONS)
def test_mem_sync_owner_flags_denied_in_any_state_and_role(mk, flag, tool, cmd):
    for state in (None, "enabled", "disabled"):
        if state:
            flag(state)
        for role in ROLES:
            res = git_policy.handle(mk(tool, role=role, command=cmd))
            assert denied(res) and "HUMAN GATE" in res.block, (state, role, cmd)


def test_owner_flag_check_runs_without_any_git_verb(mk, flag):
    flag("enabled")
    for cmd in ("mem sync --take-over", "mem sync --allow-mass-delete"):
        low = cmd.lower()
        assert "git" not in low and "gh" not in low
        assert denied(git_policy.handle(mk("PowerShell", command=cmd)))


def test_owner_flags_denied_under_deepseek(mk, flag, deepseek):
    flag("true")
    assert denied(git_policy.handle(mk("Bash", command="mem sync --take-over")))


@pytest.mark.parametrize("cmd,named,absent", [
    ("mem sync --allow-mass-delete", "--allow-mass-delete", "--take-over"),
    ("mem sync --TAKE-OVER=1", "--take-over", "--allow-mass-delete"),
    ("Start-Process mem -ArgumentList 'sync','--take-over'", "--take-over", "--allow-mass-delete"),
    ("mem sync --allow-mass-delete --take-over", "--take-over --allow-mass-delete", None),
])
def test_owner_flag_denial_names_the_flag_typed(mk, cmd, named, absent):
    res = git_policy.handle(mk("PowerShell", command=cmd))
    assert denied(res) and f"'mem sync {named}'" in res.block
    assert absent is None or absent not in res.block


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", OWNER_FLAG_ALLOWED)
def test_mem_sync_without_owner_flags_allowed(mk, flag, tool, cmd):
    flag("enabled")
    for role in ROLES:
        assert git_policy.handle(mk(tool, role=role, command=cmd)) is None, (role, cmd)
