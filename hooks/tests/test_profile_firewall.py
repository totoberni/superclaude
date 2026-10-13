"""Test della guardia profile-firewall (politica ad allowlist tra superclaude e deepseek).

HOME finta (fixture `home` di conftest): ~/.claude e ~/.claude-deepseek vivono sotto tmp_path.
Solo comandi di lettura nelle stringhe di prova.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sc_hooks import core
from sc_hooks.handlers import profile_firewall as fw
from conftest import msys, win

SC, DS = "superclaude", "deepseek"
SC_DIR, DS_DIR = ".claude", ".claude-deepseek"
VAR = "SC_" + "PROFILE"          # costruita a pezzi: il nome compare comunque nei comandi di prova


@pytest.fixture
def run(home, mk, monkeypatch):
    """run(profilo, strumento, cwd=None, transcript=None, **tool_input) -> motivo del blocco o None."""
    for rel in ("agent-memory", "plans/p", "comms", "config", "skills/s", "scripts", "hooks"):
        (home / SC_DIR / rel).mkdir(parents=True, exist_ok=True)
    for rel in ("agent-memory", "plans/p", "comms", "config", "bin", ".venv", "tmp", "projects/x"):
        (home / DS_DIR / rel).mkdir(parents=True, exist_ok=True)

    def _run(profile, tool, cwd=None, transcript=None, **ti):
        monkeypatch.setattr(core, "PROFILE", profile)
        ctx = mk(tool, cwd=None if cwd is None else str(cwd), **ti)
        if transcript is not None:
            ctx.payload["transcript_path"] = str(transcript)
        res = fw.handle(ctx)
        if res is None:
            return None
        assert res.block and "\n" not in res.block, "deny message must be one line"
        return res.block
    return _run


def sc(home: Path, rel: str = "") -> Path:
    return home / SC_DIR / rel if rel else home / SC_DIR


def ds(home: Path, rel: str = "") -> Path:
    return home / DS_DIR / rel if rel else home / DS_DIR


def junction(link: Path, target: Path) -> None:
    """Giunzione di directory (non richiede privilegi di amministratore)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {proc.stderr or proc.stdout}")


# --------------------------------------------------------------------------
# Registrazione
# --------------------------------------------------------------------------
def test_registered_on_pretooluse_for_every_tool():
    regs = [r for r in core._REGISTRY if r[2] == fw.NAME]
    assert len(regs) == 1
    event, tools, _, fn = regs[0]
    assert event == "PreToolUse" and tools == () and fn is fw.handle


def test_setup_entries_constant():
    import sc_profile
    assert sc_profile.PROFILE_SETUP_ENTRIES["deepseek"] == ("bin", ".venv", "CLAUDE.md", "skills", "agents", "rules")


# --------------------------------------------------------------------------
# A. superclaude verso ~/.claude-deepseek: solo voci di installazione
# --------------------------------------------------------------------------
DS_DENIED = ["agent-memory/.memory.db", "plans/p/plan.md", "comms/_spawns.log", "state/x", "tmp/x",
             "logs/hooks.log", "projects/x/t.jsonl", "history.jsonl", ".credentials.json", ".claude.json",
             "config/git-policy", "cache/x", "unknown-entry"]
DS_SETUP = ["bin/key-helper.py", ".venv/Scripts/python.exe", "CLAUDE.md", "skills/s/SKILL.md",
            "agents/w-x.md", "rules/00-universal.md"]


@pytest.mark.parametrize("rel", DS_DENIED)
@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "MultiEdit"])
def test_a_superclaude_denied_ds_data(run, home, tool, rel):
    reason = run(SC, tool, file_path=str(ds(home, rel)))
    assert reason and "superclaude may not access" in reason and "~/.claude-deepseek" in reason


def test_a_notebook_edit_uses_notebook_path(run, home):
    assert run(SC, "NotebookEdit", notebook_path=str(ds(home, "tmp/n.txt")), new_source="x")


@pytest.mark.parametrize("rel", DS_SETUP)
@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_a_superclaude_allowed_setup_entries(run, home, tool, rel):
    assert run(SC, tool, file_path=str(ds(home, rel))) is None


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_a_ds_settings_read_only_for_superclaude(run, home, name):
    assert run(SC, "Read", file_path=str(ds(home, name))) is None
    reason = run(SC, "Write", file_path=str(ds(home, name)), content="{}")
    assert reason and "owner-only" in reason


@pytest.mark.parametrize("tool,ti", [
    ("Glob", {"pattern": "**/*.md"}),
    ("Grep", {"pattern": "x"}),
])
def test_a_ds_home_listing_denied(run, home, tool, ti):
    assert run(SC, tool, path=str(ds(home)), **ti)


def test_a_glob_pattern_absolute_and_relative(run, home):
    assert run(SC, "Glob", pattern=str(ds(home, "agent-memory")) + "/*")
    assert run(SC, "Glob", pattern=".claude-deepseek/comms/**", path=str(home))
    assert run(SC, "Glob", pattern="bin/*.py", path=str(ds(home))) is None


def test_a_grep_without_path_uses_cwd(run, home):
    assert run(SC, "Grep", cwd=ds(home, "plans"), pattern="x")
    assert run(SC, "Grep", cwd=ds(home, "bin"), pattern="x") is None


def test_a_deny_message_names_own_alternative(run, home):
    reason = run(SC, "Read", file_path=str(ds(home, "agent-memory/.memory.db")))
    assert "use ~/.claude/agent-memory via mem" in reason


# --------------------------------------------------------------------------
# B. deepseek verso ~/.claude: solo lettura del codice condiviso
# --------------------------------------------------------------------------
SC_SHARED = ["skills/s/SKILL.md", "agents/a.md", "rules/r.md", "hooks/dispatch.py", "bin/mem",
             "scripts/memory/mem.py", "lib/sc_profile.py", "docs/d.md", "CLAUDE.md", ".venv/x", ".cache/m"]
SC_PRIVATE = ["agent-memory/.memory.db", "plans/p/plan.md", "comms/_outcomes.log", "state/s", "tmp/t",
              "logs/hooks.log", "config/git-policy", "projects/x/t.jsonl", "settings.json", "history.jsonl"]


@pytest.mark.parametrize("rel", SC_SHARED)
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_b_deepseek_reads_shared_code(run, home, tool, rel):
    key = "file_path" if tool == "Read" else "path"
    extra = {} if tool == "Read" else {"pattern": "x"}
    assert run(DS, tool, **{key: str(sc(home, rel))}, **extra) is None


@pytest.mark.parametrize("rel", SC_PRIVATE)
@pytest.mark.parametrize("tool", ["Read", "Grep"])
def test_b_deepseek_denied_private_data(run, home, tool, rel):
    key = "file_path" if tool == "Read" else "path"
    reason = run(DS, tool, **{key: str(sc(home, rel))})
    assert reason and "deepseek may not access" in reason


@pytest.mark.parametrize("rel", SC_SHARED + SC_PRIVATE)
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_b_deepseek_never_writes_under_claude(run, home, tool, rel):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    reason = run(DS, tool, **{key: str(sc(home, rel))})
    assert reason and "may not write" in reason


def test_b_claude_home_itself_denied(run, home):
    assert run(DS, "Glob", path=str(sc(home)), pattern="*")


def test_b_deny_message_names_own_alternative(run, home):
    reason = run(DS, "Read", file_path=str(sc(home, "plans/p/plan.md")))
    assert "use ~/.claude-deepseek/plans instead" in reason
    reason = run(DS, "Read", file_path=str(sc(home, "agent-memory/.memory.db")))
    assert "~/.claude-deepseek/agent-memory via mem" in reason


@pytest.mark.parametrize("rel", ["agent-memory/.memory.db", "plans/p/plan.md", "tmp/x", "CLAUDE.md"])
@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_b_deepseek_own_home_allowed(run, home, tool, rel):
    assert run(DS, tool, file_path=str(ds(home, rel))) is None


def test_b_project_local_claude_dir_is_not_the_code_home(run, home):
    assert run(DS, "Read", file_path=str(home / "code" / "proj" / ".claude" / "settings.json")) is None
    assert run(DS, "Bash", command=f"cat {msys(home)}/code/proj/.claude/notes.md") is None


# --------------------------------------------------------------------------
# C. file del proprietario
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", [SC, DS])
@pytest.mark.parametrize("which", [sc, ds])
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_c_settings_never_written(run, home, profile, which, name, tool):
    reason = run(profile, tool, file_path=str(which(home, name)))
    assert reason and ("owner-only" in reason or "may not write" in reason)


def test_c_own_settings_readable(run, home):
    assert run(SC, "Read", file_path=str(sc(home, "settings.json"))) is None
    assert run(DS, "Read", file_path=str(ds(home, "settings.json"))) is None


def test_c_deepseek_git_policy_owner_only(run, home):
    reason = run(DS, "Write", file_path=str(ds(home, "config/git-policy")), content="true")
    assert reason and "owner-only" in reason
    assert run(DS, "Read", file_path=str(ds(home, "config/git-policy"))) is None
    # Il flag superclaude resta materia di write-acl (per ruolo), non di questa guardia
    assert run(SC, "Write", file_path=str(sc(home, "config/git-policy")), content="true") is None


@pytest.mark.parametrize("profile,which", [(SC, sc), (DS, ds), (SC, ds)])
@pytest.mark.parametrize("template", [
    "echo x > {p}", "echo x >> {p}", "echo x >{p}", "Get-Content a.json | Set-Content {p}",
    "Get-Content a.json | Out-File -FilePath {p}", "cat a.json | tee {p}", "cp a.json {p}",
    "Copy-Item a.json {p}", "sed -i s/a/b/ {p}",
])
def test_c_shell_writes_to_settings(run, home, profile, which, template):
    cmd = template.format(p=win(which(home, "settings.json")))
    reason = run(profile, "PowerShell" if "-" in template.split()[0] else "Bash", command=cmd)
    assert reason and "owner-only" in reason


@pytest.mark.parametrize("profile,which", [(SC, sc), (DS, ds), (SC, ds)])
@pytest.mark.parametrize("template", [
    "cat {p}", "type {p}", "Get-Content {p}", "grep hooks {p} > out.txt", "cp {p} backup.json",
    "sed -n 1,5p {p}",
])
def test_c_shell_reads_of_settings_allowed(run, home, profile, which, template):
    assert run(profile, "Bash", command=template.format(p=which(home, "settings.json"))) is None


def test_c_shell_write_to_ds_git_policy(run, home):
    assert run(DS, "Bash", command=f"echo true > {ds(home, 'config/git-policy')}")
    assert run(DS, "Bash", command=f"cat {ds(home, 'config/git-policy')}") is None


def test_c_deepseek_shell_write_into_shared_code(run, home):
    reason = run(DS, "Bash", command=f"echo x > {sc(home, 'skills/s/SKILL.md')}")
    assert reason and "may not write" in reason
    assert run(DS, "Bash", command=f"cp {sc(home, 'skills/s/SKILL.md')} ./copy.md") is None


# --------------------------------------------------------------------------
# D. SC_PROFILE nei comandi
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", [SC, DS])
@pytest.mark.parametrize("cmd", [
    f"export {VAR}=deepseek", f"unset {VAR}", f"{VAR}=deepseek mem stats", f"env -u {VAR} mem stats",
    f"$env:{VAR} = 'deepseek'", f"Remove-Item Env:{VAR}",
    f"set {VAR}=superclaude", f"cmd /c \"set {VAR}=deepseek && mem stats\"",
    f"[Environment]::SetEnvironmentVariable('{VAR}', 'deepseek', 'User')",
    f"setx {VAR} deepseek", f"$env:{VAR}=\"deepseek\"", f"${{env:{VAR}}} = 'deepseek'", f"export {VAR}",
    f"env --unset={VAR} mem stats", f"Remove-Item -Path Env:\\{VAR}", f"$env:{VAR.lower()} = 'deepseek'",
    f"python -c \"import os; os.environ['{VAR}'] = 'deepseek'\"",
    f"python -c \"import os; os.environ.pop('{VAR}')\"", f"python -c \"import os; del os.environ['{VAR}']\"",
    f"[System.Environment]::SetEnvironmentVariable(\"{VAR}\", $null)",
])
def test_d_profile_variable_denied(run, profile, cmd):
    tool = "PowerShell" if "$env:" in cmd or "Env:" in cmd or "Environment]" in cmd or "{env:" in cmd else "Bash"
    reason = run(profile, tool, command=cmd)
    assert reason and VAR in reason


# Citare la variabile senza cambiarla e' permesso in entrambi i profili (prima: negato, falso positivo)
@pytest.mark.parametrize("profile", [SC, DS])
@pytest.mark.parametrize("cmd", [
    f"echo ${VAR}", f"echo ${{{VAR}}}", f"python -c \"import os; print(os.environ.get('{VAR}'))\"",
    f"echo $env:{VAR.lower()}", f"python -m pytest tests -q -k {VAR}", f"grep -rn {VAR} docs",
    f"git -C ~/code/proj commit -m \"feat: {VAR} selects data home\"", f"rg \"{VAR} ==\" src",
    f"Get-ChildItem Env:{VAR}", f"echo \"set the {VAR} in the launcher\"",
])
def test_d_plain_mentions_allowed(run, home, profile, cmd):
    assert run(profile, "PowerShell" if "Env:" in cmd else "Bash", cwd=home / "code", command=cmd) is None


@pytest.mark.parametrize("cmd", [
    f"grep -rn {VAR} ~/.claude/hooks", f"git -C ~/.claude commit -m \"feat: {VAR} selects data home\"",
    f"pytest -k {VAR}", f"python -m pytest ~/.claude/hooks/tests -q -k {VAR.lower()}",
])
def test_d_superclaude_mentions_in_own_repo(run, home, cmd):
    assert run(SC, "Bash", cwd=home / "code", command=cmd) is None


@pytest.mark.parametrize("cmd", [
    "cat lib/sc_profile.py", "type C:\\x\\lib\\sc_profile.py", "python -c \"import sc_profile; print(sc_profile.active_profile())\"",
    "python -c \"from sc_profile import data_home; print(data_home())\"", "grep -n def tests/test_sc_profile.py",
])
def test_d_module_name_is_not_the_variable(run, cmd):
    assert run(SC, "Bash", command=cmd) is None


# --------------------------------------------------------------------------
# E. lanciatori
# --------------------------------------------------------------------------
CLAUDE_LAUNCHES = [
    "claude", "claude -p hi", "claude.cmd --version", "claude.exe -c", "claude.ps1 --help",
    "C:\\Users\\x\\AppData\\Roaming\\npm\\claude.cmd -p hi", "/c/Users/x/AppData/Roaming/npm/claude -p hi",
    "& \"C:\\Program Files\\nodejs\\claude.exe\" -p hi", "npx claude --version", "npx -y claude -p hi",
    "npx @anthropic-ai/claude-code --version", "cmd /c claude -p hi", "cmd /c \"claude -p hi\"",
    "bash -c 'claude -p hi'", "pwsh -NoProfile -Command \"claude -p hi\"", "echo hi | claude -p",
    "git status && claude -p hi", "Start-Process claude",
]
DS_LAUNCHES = [
    "claude-ds", "claude-ds.ps1", "& ~/.claude/bin/claude-ds.ps1", "& 'C:\\Users\\x\\.claude\\bin\\claude-ds.ps1'",
    "powershell -ExecutionPolicy Bypass -File C:\\x\\claude-ds.ps1", "pwsh -File claude-ds.ps1 -p hi",
    "cmd /c claude-ds", "bash -c \"claude-ds -p hi\"",
]


@pytest.mark.parametrize("cmd", CLAUDE_LAUNCHES)
def test_e_deepseek_cannot_launch_claude(run, cmd):
    reason = run(DS, "PowerShell", command=cmd)
    assert reason and "claude CLI" in reason
    assert run(SC, "PowerShell", command=cmd) is None


@pytest.mark.parametrize("cmd", DS_LAUNCHES)
def test_e_superclaude_cannot_launch_claude_ds(run, cmd):
    reason = run(SC, "PowerShell", command=cmd)
    assert reason and "claude-ds" in reason
    assert run(DS, "PowerShell", command=cmd) is None


@pytest.mark.parametrize("cmd", ["cat claude.md", "grep -rn claude docs", "type CLAUDE.md", "ls claude-notes",
                                 "python claude_tool.py", "echo claude"])
def test_e_words_that_are_not_launches(run, cmd):
    assert run(DS, "Bash", command=cmd) is None


# --------------------------------------------------------------------------
# F. fail closed su transcript_path
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool,ti", [
    ("Read", {"file_path": "C:/x/readme.txt"}), ("Bash", {"command": "git status"}),
    ("WebSearch", {"query": "x"}), ("Agent", {"prompt": "x"}),
])
def test_f_transcript_of_other_profile_denies_everything(run, home, tool, ti):
    t_sc = sc(home, "projects/p/s.jsonl")
    t_ds = ds(home, "projects/p/s.jsonl")
    reason = run(DS, tool, transcript=t_sc, **ti)
    assert reason and "fail closed" in reason and SC in reason and DS in reason
    reason = run(SC, tool, transcript=t_ds, **ti)
    assert reason and "fail closed" in reason
    assert run(SC, tool, transcript=t_sc, **ti) is None
    assert run(DS, tool, transcript=t_ds, **ti) is None


@pytest.mark.parametrize("transcript", ["", "C:/elsewhere/t.jsonl"])
def test_f_missing_or_neutral_transcript_is_not_checked(run, transcript):
    assert run(DS, "Bash", transcript=transcript, command="git status") is None


def test_f_transcript_through_junction(run, home):
    junction(home / "code" / "tlink", ds(home, "projects"))
    assert run(SC, "Bash", transcript=home / "code" / "tlink" / "x" / "s.jsonl", command="git status")


# --------------------------------------------------------------------------
# Giunzioni
# --------------------------------------------------------------------------
def test_junction_ds_skills_to_shared_code(run, home):
    junction(ds(home, "skills"), sc(home, "skills"))
    via = ds(home, "skills/s/SKILL.md")
    assert run(DS, "Read", file_path=str(via)) is None
    reason = run(DS, "Edit", file_path=str(via))
    assert reason and "may not write" in reason
    assert run(SC, "Edit", file_path=str(via)) is None
    assert run(DS, "Bash", command=f"echo x > {via}")


def test_junction_from_workspace_into_ds_data(run, home):
    junction(home / "code" / "mlink", ds(home, "agent-memory"))
    link = home / "code" / "mlink"
    assert run(SC, "Read", file_path=str(link / ".memory.db"))
    assert run(SC, "Bash", command=f"cat {msys(link)}/.memory.db")
    assert run(SC, "Bash", cwd=link, command="sqlite3 .memory.db \"SELECT count(*) FROM memories\"")
    assert run(SC, "Read", cwd=link, file_path=".memory.db")


def test_junction_from_workspace_into_sc_data(run, home):
    junction(home / "code" / "plink", sc(home, "plans"))
    assert run(DS, "Read", file_path=str(home / "code" / "plink" / "p" / "plan.md"))
    assert run(DS, "Bash", command=f"type {win(home / 'code' / 'plink' / 'p' / 'plan.md')}")


# --------------------------------------------------------------------------
# Grafie della HOME, percorsi relativi, argomenti uniti, shell annidate
# --------------------------------------------------------------------------
def _spellings(home: Path, rel: str) -> list[str]:
    w = win(home / rel)
    tail_w = w[len(win(home)):]
    tail_f = tail_w.replace("\\", "/")
    drive = str(home)[0].lower()
    return [
        w, w.replace("\\", "/"), msys(home / rel), f"/mnt/{drive}{msys(home / rel)[2:]}",
        "~" + tail_f, "$HOME" + tail_f, "${HOME}" + tail_f, "$env:USERPROFILE" + tail_w,
        "${env:USERPROFILE}" + tail_w, "%USERPROFILE%" + tail_w, "$USERPROFILE" + tail_f,
        w.upper(), f"\"{w}\"", f"'{w}'",
    ]


@pytest.mark.parametrize("idx", range(14))
def test_spellings_superclaude_to_ds(run, home, idx):
    target = _spellings(home, ".claude-deepseek/agent-memory/.memory.db")[idx]
    assert run(SC, "Bash", command=f"cat {target}")
    assert run(SC, "Read", file_path=target)


@pytest.mark.parametrize("idx", range(14))
def test_spellings_deepseek_to_sc(run, home, idx):
    target = _spellings(home, ".claude/plans/p/plan.md")[idx]
    assert run(DS, "PowerShell", command=f"Get-Content {target}")
    assert run(DS, "Read", file_path=target)


@pytest.mark.parametrize("idx", range(14))
def test_spellings_of_allowed_paths_stay_allowed(run, home, idx):
    assert run(SC, "Bash", command=f"cat {_spellings(home, '.claude-deepseek/bin/launcher.py')[idx]}") is None
    assert run(DS, "Bash", command=f"cat {_spellings(home, '.claude/scripts/memory/mem.py')[idx]}") is None


@pytest.mark.parametrize("cwd_rel,cmd", [
    (".claude-deepseek", "cat agent-memory/x.txt"),
    (".claude-deepseek/bin", "cat ../plans/p/plan.md"),
    ("", "cat .claude-deepseek/comms/_spawns.log"),
    ("code", "cat ../.claude-deepseek/tmp/x"),
    (".claude-deepseek", "ls plans"),
    ("code", "cd ~/.claude-deepseek && cat comms/x.log"),
    ("code", "cd ~/.claude-deepseek/agent-memory; ls"),
])
def test_relative_paths_superclaude(run, home, cwd_rel, cmd):
    assert run(SC, "Bash", cwd=home / cwd_rel if cwd_rel else home, command=cmd)


@pytest.mark.parametrize("cwd_rel,cmd", [
    (".claude", "cat plans/p/plan.md"),
    (".claude/scripts", "cat ../agent-memory/x"),
    ("", "type .claude\\comms\\x.log"),
    (".claude", "ls comms"),
])
def test_relative_paths_deepseek(run, home, cwd_rel, cmd):
    assert run(DS, "Bash", cwd=home / cwd_rel if cwd_rel else home, command=cmd)


def test_relative_paths_allowed(run, home):
    assert run(SC, "Bash", cwd=ds(home, "bin"), command="cat launcher.py") is None
    assert run(DS, "Bash", cwd=sc(home, "scripts"), command="python memory/mem.py stats") is None
    # `git status` con cwd in ~/.claude/scripts ora e' negato (regola B, git): vedi test_s3_*
    assert run(SC, "Read", cwd=ds(home), file_path="bin/launcher.py") is None
    assert run(SC, "Read", cwd=ds(home), file_path="plans/p/plan.md")


@pytest.mark.parametrize("cmd", [
    "sqlite3 --db={p} .tables", "python tool.py --db={p}", "python tool.py --db=\"{p}\"",
    "Get-Content -Path:{p}", "DB={p} python tool.py", "python tool.py -o {p}",
])
def test_joined_arguments(run, home, cmd):
    assert run(SC, "Bash", command=cmd.format(p=ds(home, "agent-memory/.memory.db")))
    assert run(DS, "Bash", command=cmd.format(p=sc(home, "agent-memory/.memory.db")))


@pytest.mark.parametrize("template", [
    "bash -c \"cat {p}\"", "bash -c 'cat {p}'", "sh -c \"head -5 {p}\"",
    "cmd /c type {p}", "cmd /c \"type {p}\"", "powershell -Command \"Get-Content {p}\"",
    "pwsh -c 'Get-Content {p}'", "pwsh -NoProfile -Command \"bash -c 'cat {p}'\"",
    "python -c \"print(open(r'{p}').read())\"",
    "python - <<'EOF'\nprint(open(r'{p}').read())\nEOF",
])
def test_nested_shells_and_strings(run, home, template):
    assert run(SC, "Bash", command=template.format(p=win(ds(home, "plans/p/plan.md"))))
    assert run(DS, "Bash", command=template.format(p=win(sc(home, "comms/_spawns.log"))))


@pytest.mark.parametrize("profile,cmd", [
    (SC, "python -c \"print(open('.claude-deepseek/agent-memory/x').read())\""),
    (SC, "grep -c x .claude-deepseek\\plans\\p\\plan.md"),
    (DS, "python -c \"import pathlib; print(pathlib.Path('.claude\\\\plans').exists())\""),
    (DS, "sqlite3 x.db \"ATTACH '.claude/agent-memory/.memory.db' AS m\""),
    (SC, "cat ~/.claude-deepseek/bin/../agent-memory/x"),
])
def test_bare_substrings_without_home_prefix(run, home, profile, cmd):
    # Un `.claude/` nudo e' la home del codice solo con la cwd nella HOME (N1): qui cwd = HOME
    assert run(profile, "Bash", cwd=home, command=cmd)


@pytest.mark.parametrize("cwd_rel", ["code", "code/proj"])
@pytest.mark.parametrize("cmd", [
    "git -C ~/code/proj diff -- .claude/settings.json",
    "python -c \"import pathlib; print(pathlib.Path('.claude/settings.json').exists())\"",
])
def test_n1_bare_claude_dir_away_from_home_is_a_project_dir(run, home, cwd_rel, cmd):
    assert run(DS, "Bash", cwd=home / cwd_rel, command=cmd) is None


def test_glob_tokens_in_shell(run, home):
    assert run(SC, "Bash", command=f"ls {msys(ds(home))}/*")
    assert run(SC, "Bash", command=f"cat {msys(ds(home))}/agent-memory/*.db")
    assert run(DS, "Bash", command=f"ls {msys(sc(home))}/{{plans,comms}}")
    assert run(SC, "Bash", command=f"ls {msys(ds(home))}/bin/*.py") is None


# --------------------------------------------------------------------------
# Neutralita': lavoro superclaude normale (SC_PROFILE assente)
# --------------------------------------------------------------------------
SUPERCLAUDE_COMMANDS = [
    "git -C ~/.claude status --short", "git -C C:/Users/x/code/proj log --oneline -5",
    "mem search \"profile firewall gotchas\" -k 8", "python ~/.claude/scripts/memory/mem.py stats",
    "cat ~/.claude/plans/deepseek/plan.md", "type %USERPROFILE%\\.claude\\comms\\_spawns.log",
    "Get-Content $env:USERPROFILE\\.claude\\agent-memory\\notes.txt -TotalCount 5",
    "sqlite3 ~/.claude/agent-memory/.memory.db \"SELECT name FROM memories LIMIT 3\"",
    "python -m pytest C:/Users/x/.claude/hooks/tests -q -p no:cacheprovider",
    "python -c \"print(open('C:/Users/x/.claude/CLAUDE.md').read()[:80])\"",
    "grep -rn \"def handle\" ~/.claude/hooks/sc_hooks/handlers | head -20",
    "ls ~/.claude/skills && ls ~/code", "echo done 2>&1 > /dev/null", "uv pip list",
    "cd ~/code/proj && git diff --stat", "claude --version", "npx claude --help",
    "grep -n claude-deepseek ~/.claude/plans/deepseek/plan.md",
    "grep -rn \"\\.claude-deepseek\" ~/.claude/plans/deepseek",
    "cat ~/.claude-deepseek/CLAUDE.md", "ls ~/.claude-deepseek/bin",
    "Get-Content ~/.claude-deepseek/settings.json",
    "cat ~/code/proj/.claude/settings.json",
    "python ~/.claude/scripts/plan/render_plan.py ~/.claude/plans/deepseek/plan.md",
]


@pytest.mark.parametrize("cmd", SUPERCLAUDE_COMMANDS)
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_superclaude_commands_untouched(run, home, cmd, tool):
    assert run(SC, tool, cwd=home / "code", command=cmd) is None


@pytest.mark.parametrize("tool,ti", [
    ("Read", {"file_path": "~/.claude/plans/deepseek/plan.md"}),
    ("Edit", {"file_path": "~/.claude/hooks/sc_hooks/core.py"}),
    ("Write", {"file_path": "~/.claude/tmp/x.md", "content": "x"}),
    ("Write", {"file_path": "~/code/proj/a.py", "content": "x"}),
    ("Glob", {"pattern": "**/*.py", "path": "~/.claude/hooks"}),
    ("Grep", {"pattern": "x", "path": "~/code"}),
    ("Grep", {"pattern": "x"}),
    ("WebSearch", {"query": "x"}),
])
def test_superclaude_file_work_untouched(run, home, tool, ti):
    assert run(SC, tool, transcript=sc(home, "projects/p/s.jsonl"), **ti) is None


def test_unknown_profile_is_inert(run):
    assert run("", "Bash", command="cat ~/.claude-deepseek/agent-memory/x") is None


# --------------------------------------------------------------------------
# Destinazioni con nome (Copy-Item -Destination, cp -t, -o/--output)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", [SC, DS])
@pytest.mark.parametrize("which", [sc, ds])
@pytest.mark.parametrize("template", [
    "Copy-Item -Destination {p} -Path ./x.json", "Copy-Item -Dest {p} -Path ./x.json",
    "Copy-Item -des {p} x.json", "Copy-Item -DESTINATION:{p} -Path x.json",
    "Move-Item -Path ./x.json -Destination {p}", "cp -t {d} settings.json",
    "cp --target-directory={d} settings.json", "cp --target-directory {d} ./settings.json",
    "cp settings.json {d}/", "Copy-Item ./settings.json -Destination {d}",
    "curl -o {p} https://example.com/x", "python tool.py --output={p}", "sort --output {p} a.txt",
])
def test_s2_named_destination_to_settings(run, home, profile, which, template):
    cmd = template.format(p=which(home, "settings.json"), d=which(home))
    reason = run(profile, "PowerShell" if "-" in template.split()[0] else "Bash", cwd=home / "code", command=cmd)
    # Con {d} = ~/.claude-deepseek la home stessa non e' una voce di installazione: negata gia' dalla regola A
    allowed_dir_deny = "{d}" in template and profile == SC and which is ds
    assert reason and ("owner-only" in reason or "may not write" in reason
                       or (allowed_dir_deny and "superclaude may not access ~/.claude-deepseek:" in reason))


@pytest.mark.parametrize("cmd", [
    "Copy-Item -Destination ~/.claude/hooks -Path ./x.py", "Copy-Item -Destination ~/.claude/hooks/x.py ./x.py",
    "cp -t ~/.claude/hooks x.py", "Invoke-WebRequest https://example.com/x -OutFile ~/.claude/hooks/x.py",
    "Expand-Archive x.zip -DestinationPath ~/.claude/skills",
])
def test_s2_deepseek_named_destination_into_shared_code(run, home, cmd):
    reason = run(DS, "PowerShell", cwd=home / "code", command=cmd)
    assert reason and "may not write" in reason


@pytest.mark.parametrize("cmd", [
    "Copy-Item -Path ~/.claude/hooks/dispatch.py -Destination ./x.py", "cp -t ./out ~/.claude/skills/s/SKILL.md",
    "Copy-Item -Destination ./out -Path ~/.claude/rules/r.md", "grep -o hooks ~/.claude/hooks/dispatch.py",
    "cp ~/.claude/scripts/x.py ./x.py",
])
def test_s2_deepseek_copies_out_of_shared_code_allowed(run, home, cmd):
    assert run(DS, "PowerShell", cwd=home / "code", command=cmd) is None


def test_s2_setup_entries_still_writable_by_superclaude(run, home):
    assert run(SC, "Bash", cwd=home / "code", command="cp -t ~/.claude-deepseek/bin launcher.py") is None
    assert run(SC, "PowerShell", cwd=home / "code",
               command="Copy-Item -Destination ~/.claude-deepseek/bin -Path ./launcher.py") is None
    assert run(SC, "Bash", cwd=home / "code", command="cp -t ~/.claude-deepseek/plans x.md")


# --------------------------------------------------------------------------
# git e tar nel repository della home del codice
# --------------------------------------------------------------------------
@pytest.mark.parametrize("cwd_rel,cmd", [
    ("code", "git -C ~/.claude/hooks checkout -- dispatch.py"), ("code", "git -C ~/.claude log --oneline -3"),
    ("code", "git -C ~/.claude/skills status"), (".claude/hooks", "git checkout -- dispatch.py"),
    (".claude/scripts", "git status"), ("code", "cd ~/.claude/hooks && git status"),
    ("code", "tar -xf x.tar -C ~/.claude/hooks"), ("code", "tar -xf x.tar --directory=~/.claude/hooks"),
    ("code", "tar -x -f x.tar --directory ~/.claude/skills"),
])
def test_s3_deepseek_git_and_tar_into_code_home(run, home, cwd_rel, cmd):
    assert run(DS, "Bash", cwd=home / cwd_rel, command=cmd)


def test_s3_git_message_names_the_repository(run, home):
    reason = run(DS, "Bash", cwd=sc(home, "hooks"), command="git checkout -- dispatch.py")
    assert reason and "may not run git" in reason and "~/.claude repository" in reason


@pytest.mark.parametrize("cwd_rel,cmd", [
    ("code", "git -C ~/code/proj status"), ("code/proj", "git status"), ("code", "git -C ~/.claude-deepseek status"),
    ("code", "tar -xf x.tar -C ./out"), ("code", "tar -cf out.tar -C ./src ."),
])
def test_s3_deepseek_git_elsewhere_allowed(run, home, cwd_rel, cmd):
    assert run(DS, "Bash", cwd=home / cwd_rel, command=cmd) is None


def test_s3_superclaude_git_and_tar(run, home):
    assert run(SC, "Bash", cwd=sc(home, "hooks"), command="git checkout -- dispatch.py") is None
    assert run(SC, "Bash", cwd=home / "code", command="tar -xf x.tar -C ~/.claude/hooks") is None
    assert run(SC, "Bash", cwd=home / "code", command="tar -xf x.tar -C ~/.claude-deepseek/bin") is None
    assert run(SC, "Bash", cwd=home / "code", command="tar -xf x.tar -C ~/.claude-deepseek/plans")


# --------------------------------------------------------------------------
# Ambiente svuotato (deepseek)
# --------------------------------------------------------------------------
ENV_DROPS = [
    "env -i USERPROFILE=$USERPROFILE mem search x", "env --ignore-environment mem stats", "env - mem stats",
    "env -u FOO -i mem stats", "env FOO=1 -i mem stats", "Start-Process mem -UseNewEnvironment",
    "Start-Process -FilePath mem -ArgumentList stats -UseNewEnv", "bash -c 'env -i mem stats'",
]


@pytest.mark.parametrize("cmd", ENV_DROPS)
def test_s4_deepseek_cannot_drop_the_environment(run, home, cmd):
    reason = run(DS, "PowerShell" if "Start-Process" in cmd else "Bash", cwd=home / "code", command=cmd)
    assert reason and "emptied environment" in reason
    assert run(SC, "PowerShell" if "Start-Process" in cmd else "Bash", cwd=home / "code", command=cmd) is None


@pytest.mark.parametrize("cmd", ["env FOO=1 python -i x.py", "printenv PATH", "echo $env:PATH",
                                 "Start-Process notepad", "env | sort"])
def test_s4_environment_use_allowed(run, home, cmd):
    assert run(DS, "Bash", cwd=home / "code", command=cmd) is None


# --------------------------------------------------------------------------
# Comandi oltre MAX_CMD, fail closed su errore interno
# --------------------------------------------------------------------------
@pytest.mark.parametrize("profile", [SC, DS])
def test_s5_over_long_command_denied(run, home, profile):
    from sc_hooks.handlers import _util
    cmd = "echo " + "x" * _util.MAX_CMD + " ; cat ~/.claude-deepseek/plans/p/plan.md"
    reason = run(profile, "Bash", cwd=home / "code", command=cmd)
    assert reason and "exceeds" in reason and str(_util.MAX_CMD) in reason
    assert run(profile, "Bash", cwd=home / "code", command="echo " + "x" * (_util.MAX_CMD - 5)) is None


def test_s6_internal_error_fails_closed_only_outside_default_profile(run, home, monkeypatch):
    logged: list[str] = []

    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(fw, "_shell_reason", boom)
    monkeypatch.setattr(core, "log_error", lambda where, exc: logged.append(f"{where}:{exc}"))
    reason = run(DS, "Bash", cwd=home / "code", command="git status")
    assert reason and "fail closed" in reason and "RuntimeError" in reason
    assert run(SC, "Bash", cwd=home / "code", command="git status") is None
    assert logged == [f"{fw.NAME}:boom", f"{fw.NAME}:boom"]



# --------------------------------------------------------------------------
# W2-A1-A3-A6-A10: file privati, forme device/UNC, chiave DeepSeek, Monitor, riserva fail-open
# --------------------------------------------------------------------------
def fill(text: str, home: Path) -> str:
    fwd = str(home).replace("\\", "/")
    return text.replace("{home}", fwd).replace("{win}", win(home)).replace("{msys}", msys(home))


PRIVATE = [".claude.json", ".claude.json.backup", ".claude.json.backup.1712345678", ".CLAUDE.JSON"]


@pytest.mark.parametrize("name", PRIVATE)
@pytest.mark.parametrize("tool", ["Read", "Write", "Edit"])
def test_a1_private_file_denied_to_deepseek_only(run, home, tool, name):
    p = home / name
    for spelled in (str(p), win(p), msys(p), str(p).replace("\\", "/")):
        reason = run(DS, tool, file_path=spelled)
        assert reason and "private file of the superclaude profile" in reason, spelled
        assert run(SC, tool, file_path=spelled) is None, spelled


A1_SHELL = [
    ("Bash", "cat ~/.claude.json"), ("Bash", "cat $HOME/.claude.json"), ("Bash", "cat ${HOME}/.claude.json.backup"),
    ("Bash", 'cat "{home}/.claude.json"'), ("Bash", "cat {msys}/.claude.json"), ("Bash", "cp ~/.claude.json /tmp/x"),
    ("Bash", "echo x > ~/.claude.json"), ("Bash", "cd ~ && cat .claude.json"),
    ("Bash", "python -c \"print(open(r'{win}\\.claude.json').read())\""),
    ("PowerShell", "Get-Content ~\\.claude.json"), ("PowerShell", "Get-Content $env:USERPROFILE\\.claude.json"),
    ("PowerShell", "Get-Content ${env:USERPROFILE}/.claude.json.backup.17"),
    ("PowerShell", "type %USERPROFILE%\\.claude.json"), ("PowerShell", "Get-Content '{win}\\.claude.json'"),
    ("Monitor", "tail -f ~/.claude.json"),
]


@pytest.mark.parametrize("tool,cmd", A1_SHELL)
def test_a1_private_file_shell_spellings(run, home, tool, cmd):
    command = fill(cmd, home)
    reason = run(DS, tool, cwd=home / "code", command=command)
    assert reason and "private file of the superclaude profile" in reason
    assert run(SC, tool, cwd=home / "code", command=command) is None


def test_a1_bare_name_counts_only_with_cwd_at_home(run, home):
    assert run(DS, "Bash", cwd=home, command="cat .claude.json")
    assert run(DS, "Bash", cwd=home / "code" / "proj", command="cat .claude.json") is None


@pytest.mark.parametrize("tool,ti", [
    ("Read", {"file_path": "{home}/.claude-deepseek/.claude.json"}),
    ("Write", {"file_path": "{home}/.claude-deepseek/.claude.json"}),
    ("Read", {"file_path": "{home}/code/proj/.claude.json"}),
    ("Read", {"file_path": "{home}/.claude.jsonx"}),
    ("Bash", {"command": "cat ~/.claude-deepseek/.claude.json"}),
    ("Bash", {"command": "cat ~/code/proj/.claude.json ./.claude.json"}),
])
def test_a1_deepseek_own_and_project_claude_json_allowed(run, home, tool, ti):
    assert run(DS, tool, cwd=home / "code" / "proj", **{k: fill(v, home) for k, v in ti.items()}) is None


def _short(p: Path) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    return buf.value if n else ""


def test_a1_short_name(run, home):
    cj = home / ".claude.json"
    cj.write_text("{}")
    short = _short(cj)
    if "~" not in short.rsplit("\\", 1)[-1]:
        pytest.skip("8.3 names disabled")
    assert run(DS, "Read", file_path=short)
    assert run(DS, "Bash", cwd=home / "code", command=f'type "{short}"')
    assert run(SC, "Read", file_path=short) is None


def device_forms(p: Path) -> dict[str, str]:
    w = win(p)
    d, rest = w[0], w[2:]
    return {"qmark": "\\\\?\\" + w, "dot": "\\\\.\\" + w, "unc-qmark": "\\\\?\\UNC\\localhost\\" + d + "$" + rest,
            "localhost": "\\\\localhost\\" + d + "$" + rest, "loopback": "\\\\127.0.0.1\\" + d + "$" + rest,
            "fwd-qmark": "//?/" + w.replace("\\", "/"), "fwd-localhost": "//localhost/" + d + "$" + rest.replace("\\", "/")}


@pytest.mark.parametrize("form", list(device_forms(Path("C:/x"))))
@pytest.mark.parametrize("profile,which,rel", [
    (DS, sc, "agent-memory/.memory.db"), (SC, ds, "agent-memory/.memory.db"), (DS, sc, "plans/p/plan.md"),
    (SC, ds, "plans/p/plan.md"),
])
def test_a3_device_and_unc_forms_denied(run, home, form, profile, which, rel):
    path = device_forms(which(home, rel))[form]
    assert run(profile, "Read", file_path=path), path
    assert run(profile, "Bash", cwd=home / "code", command=f"cat '{path}'"), path
    assert run(profile, "PowerShell", cwd=home / "code", command=f'Get-Content "{path}"'), path


@pytest.mark.parametrize("form", list(device_forms(Path("C:/x"))))
def test_a3_device_forms_of_private_file(run, home, form):
    path = device_forms(home / ".claude.json")[form]
    assert run(DS, "Read", file_path=path), path
    assert run(DS, "Bash", cwd=home / "code", command=f"cat '{path}'"), path
    assert run(SC, "Read", file_path=path) is None


@pytest.mark.parametrize("form", list(device_forms(Path("C:/x"))))
@pytest.mark.parametrize("profile,which,rel", [
    (DS, sc, "skills/s/SKILL.md"), (SC, sc, "agent-memory/.memory.db"), (DS, ds, "agent-memory/.memory.db"),
    (SC, ds, "bin/mem"),
])
def test_a3_device_forms_keep_allowed_access(run, home, form, profile, which, rel):
    path = device_forms(which(home, rel))[form]
    assert run(profile, "Read", file_path=path) is None, path
    assert run(profile, "Bash", cwd=home / "code", command=f"cat '{path}'") is None, path


def test_a3_device_form_cwd(run, home):
    cwd = device_forms(sc(home, "agent-memory"))["qmark"]
    assert run(DS, "Bash", cwd=cwd, command="cat .memory.db")


KEY_CMDS = [
    ("Bash", "python ~/.claude-deepseek/bin/key-helper.py"), ("Bash", "cat ~/.claude/bin/key-helper.py"),
    ("PowerShell", "& $HOME\\.claude-deepseek\\bin\\key-helper.ps1"), ("Bash", "key-helper"),
    ("Bash", "keyring get claude-deepseek api"), ("Bash", "python -m keyring get claude-deepseek api"),
    ("Bash", "python -c \"import keyring; print(keyring.get_password('claude-deepseek', 'api'))\""),
    ("Bash", "python -c \"import keyring as k; k.get_credential('claude-deepseek', None)\""),
    ("PowerShell", "cmdkey /list:claude-deepseek"), ("PowerShell", "Get-StoredCredential -Target claude-deepseek"),
    ("PowerShell", "Get-StoredCredential"), ("Bash", "python store-key.py"),
    ("Bash", "py -3 ~/.claude/scripts/store-key.py"), ("Bash", "./store-key.py"), ("Bash", "uv run store-key.py"),
    ("Bash", "bash -c 'python store-key.py'"), ("PowerShell", "python3.12 C:\\x\\store-key.py --force"),
    ("Monitor", "python store-key.py"), ("Monitor", "keyring get claude-deepseek api"),
]


@pytest.mark.parametrize("profile", [SC, DS])
@pytest.mark.parametrize("tool,cmd", KEY_CMDS)
def test_a6_key_access_denied_in_both_profiles(run, home, profile, tool, cmd):
    reason = run(profile, tool, cwd=home / "code", command=cmd)
    assert reason and "the DeepSeek key is never read by agents" in reason


@pytest.mark.parametrize("profile", [SC, DS])
@pytest.mark.parametrize("cmd", [
    "keyring --list-backends", "keyring get other-service api", "cmdkey /list", "git add store-key.py",
    "grep -n set store-key.py", "python -c \"import keyring; keyring.get_password('other', 'x')\"",
])
def test_a6_unrelated_keyring_and_store_key_mentions_allowed(run, home, profile, cmd):
    assert run(profile, "Bash", cwd=home / "code", command=cmd) is None


@pytest.mark.parametrize("profile,which", [(DS, sc), (SC, ds)])
def test_monitor_is_screened_like_bash(run, home, profile, which):
    cmd = f"cat {which(home, 'agent-memory/.memory.db')}"
    assert run(profile, "Monitor", cwd=home / "code", command=cmd, description="m")
    assert run(profile, "Bash", cwd=home / "code", command=cmd)


@pytest.mark.parametrize("profile", [SC, DS])
def test_monitor_without_command_passes(run, home, profile):
    assert run(profile, "Monitor", cwd=home / "code", description="m") is None
    assert run(profile, "Monitor", cwd=home / "code", command="tail -f build.log") is None


def test_a10_superclaude_fail_open_except_ds_home_mentions(run, home, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(fw, "_reason", boom)
    monkeypatch.setattr(core, "log_error", lambda where, exc: None)
    reason = run(SC, "Bash", cwd=home / "code", command="cat ~/.claude-deepseek/plans/p/plan.md")
    assert reason and "fail closed" in reason and "RuntimeError" in reason
    assert run(SC, "Read", file_path=win(home) + "\\.Claude-DeepSeek\\x")
    assert run(SC, "Monitor", command="tail ~/.claude-deepseek/tmp/x")
    assert run(SC, "Bash", cwd=home / "code", command="cat ~/.claude/plans/p/plan.md") is None
    assert run(SC, "Read", file_path=str(home / "code" / "x.py")) is None
