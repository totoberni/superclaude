"""Test dell'avviso cd-note: solo note, mai blocchi né riscritture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import msys, win
from sc_hooks import core
from sc_hooks.handlers import cd_note

SHELLS = ("Bash", "PowerShell")
EM, EN = chr(0x2014), chr(0x2013)


def text(res) -> str:
    assert res is None or (not res.block and res.updated_input is None)   # mai blocchi
    return "\n".join(res.context) if res else ""


def run(mk, cmd, tool="Bash", **kw) -> str:
    return text(cd_note.handle(mk(tool, command=cmd, **kw)))


def dest(note: str) -> str:
    """Directory di arrivo citata nella nota."""
    return note.split(" -> ", 1)[1].split(". Keep track", 1)[0]


# --------------------------------------------------------------------------
# Spostamenti persistenti: nota attesa
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd,to", [
    ("cd /c/x && ls", "c:/x"),
    ('cd "C:/a b"; git status', "c:/a b"),
    ("CD /c/x", "c:/x"),
    ("cd /c/x 2>/dev/null || exit 1", "c:/x"),
    ("cd -P /c/x", "c:/x"),
    ("cd /c/x && make  # it's fine", "c:/x"),
    ("{ cd /c/x; ls; }", "c:/x"),
    ("if [ -d /c/x ]; then cd /c/x; fi", "c:/x"),
    ("cd /c/x && make && cd /c/y", "c:/y"),
    ("cd /c/x; cd ../y", "c:/y"),
    ("cd -", "the previous directory"),
    ("popd", "the previous directory"),
    ('cd "$(git rev-parse --show-toplevel)"', "$(git rev-parse --show-toplevel)"),
])
@pytest.mark.parametrize("tool", SHELLS)
def test_persistent_cd_notes_destination(mk, tool, cmd, to):
    out = run(mk, cmd, tool)
    assert out.startswith("cd-note: ") and "\n" not in out
    assert dest(out) == to


@pytest.mark.parametrize("cmd,to", [
    (r"Set-Location C:\x", "c:/x"),
    (r"sl C:\x", "c:/x"),
    (r"SET-LOCATION -Path 'C:\a b'", "c:/a b"),
    (r"Set-Location -LiteralPath C:\x; Get-ChildItem", "c:/x"),
    (r"Push-Location C:\x", "c:/x"),
    ("Pop-Location", "the previous directory"),
    ("Set-Location -", "the previous directory"),
])
def test_powershell_forms(mk, cmd, to):
    assert dest(run(mk, cmd, "PowerShell")) == to


def test_from_is_payload_cwd_and_restore_hint(mk, home):
    start = core.norm_path(home / "code" / "proj")
    out = run(mk, "cd /c/x && ls")
    assert f": {start} -> c:/x." in out
    assert f"move back with `cd {start}` when done" in out
    assert "`( cd <dir> && ... )` runs without moving." in out


def test_from_with_spaces_is_quoted_in_hint(mk):
    out = run(mk, "cd /c/x", cwd="C:/my dir/x")
    assert 'cd "c:/my dir/x"' in out


@pytest.mark.parametrize("cwd_form", [str, win, msys])
def test_relative_cd_resolves_against_cwd(mk, home, cwd_form):
    out = run(mk, "cd sub && ls", cwd=cwd_form(home / "code" / "proj"))
    assert dest(out) == core.norm_path(home / "code" / "proj" / "sub")


def test_pushd_notes(mk):
    assert dest(run(mk, "pushd /tmp")).endswith("/tmp")


@pytest.mark.parametrize("role", ["plain", "meta", "worker"])
def test_every_role_gets_the_note(mk, role):
    assert dest(run(mk, "cd /c/x", role=role)) == "c:/x"


# --------------------------------------------------------------------------
# Nessuna nota
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "( cd /c/x && ls )", "(cd /c/x && ls)", "ls && (cd /c/x; make)", "( cd /c/x && cd /c/y )",
    'bash -c "cd /c/x && ls"', "sh -c 'cd /c/x; make'", r'powershell -Command "Set-Location C:\x; ls"',
    "echo $(cd /c/x; pwd)", "x=$( cd /c/x; pwd )", "echo $(pwd; cd /c/x)",
])
@pytest.mark.parametrize("tool", SHELLS)
def test_non_persistent_cd_is_silent(mk, tool, cmd):
    assert cd_note.handle(mk(tool, command=cmd)) is None


@pytest.mark.parametrize("cmd", [
    "git -C /c/x status", "ls", "abcd x", "cdk deploy", "slx foo", "echo cd /c/x", 'echo "cd /c/x"',
    "git commit -m 'cd somewhere'",
])
@pytest.mark.parametrize("tool", SHELLS)
def test_commands_without_cd_are_silent(mk, tool, cmd):
    assert cd_note.handle(mk(tool, command=cmd)) is None


def _restore_targets(home: Path) -> list[str]:
    proj = home / "code" / "proj"
    return [
        "cd ~", "cd $HOME", 'cd "${HOME}"', "cd ~/code", "cd ..", "cd .", "cd",
        f"cd {msys(proj)}", f'cd "{win(proj)}"', f"cd {msys(home)}", f"cd {msys(home / 'code')}",
        f"Set-Location {win(proj)}", "Set-Location $env:USERPROFILE", "sl %USERPROFILE%",
    ]


@pytest.mark.parametrize("idx", range(14))
@pytest.mark.parametrize("tool", SHELLS)
def test_staying_or_going_home_is_silent(mk, home, tool, idx):
    cmd = _restore_targets(home)[idx]
    assert cd_note.handle(mk(tool, command=cmd)) is None, cmd


@pytest.mark.parametrize("cmd", [
    "cd /c/x && make && cd -",                       # cd - torna a OLDPWD, cioè la partenza
    "pushd /c/x && make && popd",
    r"Push-Location C:\x; make; Pop-Location",
    "cd /c/x; make; cd ~/code",
    "cd /c/x; make; cd",                             # cd nudo riconosciuto: torna alla home
])
@pytest.mark.parametrize("tool", SHELLS)
def test_command_that_moves_back_is_silent(mk, tool, cmd):
    assert cd_note.handle(mk(tool, command=cmd)) is None


# --------------------------------------------------------------------------
# Input malformato: None, mai un'eccezione (il dispatcher la ingoierebbe)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ti", [{}, {"command": ""}, {"command": "   "}, {"command": None}, {"command": 3.5},
                                {"command": {"a": 1}}, {"command": ["cd", "/c/x"]}, {"command": 'cd "C:/a b'},
                                {"command": "cd 'unterminated"}])
@pytest.mark.parametrize("tool", SHELLS)
def test_malformed_input(mk, tool, ti):
    assert cd_note.handle(mk(tool, tool_input=ti)) is None


def test_malformed_ctx(mk):
    assert cd_note.handle(mk("Bash", tool_input=[])) is None
    assert cd_note.handle(mk("Bash", command="cd /c/x", cwd="")) is None
    ctx = mk("Bash", command="cd /c/x")
    ctx.cwd = None
    assert cd_note.handle(ctx) is None


# --------------------------------------------------------------------------
# Registrazione, dispatch e purezza del sorgente
# --------------------------------------------------------------------------
def test_registered_as_warn_on_shell_tools():
    assert any(n == "cd-note" and e == "PreToolUse" and set(t) == set(core.SHELL_TOOLS)
               for e, t, n, _ in core._REGISTRY)
    assert core._DEFAULT_MODE["cd-note"] == "warn"


@pytest.mark.parametrize("mode", ["", "warn", "block"])
@pytest.mark.parametrize("tool", SHELLS)
def test_dispatch_note_never_exits_2(mk, monkeypatch, capsys, tool, mode):
    monkeypatch.setattr(core, "_REGISTRY", [("PreToolUse", core.SHELL_TOOLS, "cd-note", cd_note.handle)])
    monkeypatch.setenv("SUPERCLAUDE_GUARD_CD_NOTE", mode)
    assert core.dispatch(mk(tool, command="cd /c/x && ls")) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)["hookSpecificOutput"]
    assert out["additionalContext"].startswith("cd-note: ") and "updatedInput" not in out
    assert "GUARD-BLOCK" not in captured.err


def test_note_is_ascii_and_source_has_no_dashes(mk):
    assert run(mk, "cd /c/x && ls").isascii()
    src = Path(cd_note.__file__).read_text(encoding="utf-8")
    assert EM not in src and EN not in src
