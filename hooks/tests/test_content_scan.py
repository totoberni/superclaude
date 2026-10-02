"""Test della guardia content-scan. I trattini vietati sono costruiti con chr()."""
from __future__ import annotations

import pytest
from conftest import msys, win

from sc_hooks.handlers import content_scan

EM, EN = chr(0x2014), chr(0x2013)


def denied(res):
    return res is not None and bool(res.block)


def fixed(res, key="content"):
    """Testo corretto restituito dalla guardia, oppure None se non ha riscritto nulla."""
    if res is None or res.updated_input is None:
        return None
    assert not res.block, "i trattini si correggono, non si bloccano"
    return res.updated_input.get(key)


def test_em_dash_becomes_semicolon_in_every_write_shape(mk, home):
    p = win(home / "notes.md")
    text = f"alpha {EM} beta"
    assert fixed(content_scan.handle(mk("Write", file_path=p, content=text))) == "alpha; beta"
    assert fixed(content_scan.handle(mk("Edit", file_path=p, old_string="a", new_string=text)), "new_string") == "alpha; beta"
    assert fixed(content_scan.handle(mk("NotebookEdit", notebook_path=p, new_source=text)), "new_source") == "alpha; beta"
    res = content_scan.handle(mk("MultiEdit", file_path=p, edits=[{"new_string": "ok"}, {"old_string": "z", "new_string": text}]))
    assert [e["new_string"] for e in fixed(res, "edits")] == ["ok", "alpha; beta"]
    assert fixed(res, "edits")[1]["old_string"] == "z"          # il resto dell'edit resta intatto


SUBSTITUTIONS = [
    ("a {EM} b", "a; b"),                      # inciso con spazi
    ("a{EM}b", "a; b"),                        # inciso senza spazi
    ("end {EM}", "end;"),                      # in coda alla riga: niente spazio finale
    ("x {EM} y {EM} z", "x; y; z"),
    ("pages 10{EN}20", "pages 10-20"),         # intervallo numerico
    ("a {EN} b", "a; b"),                      # trattino medio usato come inciso
    ("north{EN}south", "north-south"),
    ("keep\nline {EM} two\n", "keep\nline; two\n"),
    ("crlf {EM} line\r\nnext", "crlf; line\r\nnext"),   # i fine riga CRLF restano CRLF
]


@pytest.mark.parametrize("src,want", SUBSTITUTIONS)
def test_substitution_rules(mk, home, src, want):
    src = src.format(EM=EM, EN=EN)
    res = content_scan.handle(mk("Write", file_path=win(home / "n.md"), content=src))
    assert fixed(res) == want


def test_rewrite_keeps_other_fields_and_notes_the_model(mk, home):
    p = win(home / "notes.md")
    res = content_scan.handle(mk("Write", file_path=p, content=f"a {EM} b {EN} c"))
    assert res.updated_input["file_path"] == p                   # input COMPLETO, non solo il campo cambiato
    assert res.context and "replaced 2" in res.context[0]


def test_agent_prompts_are_not_scanned(mk):
    """Un prompt di dispatch non è un documento: nessuna riscrittura, nessun blocco."""
    assert content_scan.handle(mk("Agent", prompt=f"review {EM} carefully")) is None


def test_dash_only_in_old_string_is_fine(mk, home):
    ctx = mk("Edit", file_path=win(home / "a.md"), old_string=f"x {EM} y", new_string="x; y")
    assert content_scan.handle(ctx) is None


def test_hyphen_and_plain_text_pass(mk, home):
    assert content_scan.handle(mk("Write", file_path=win(home / "a.md"), content="a - b -- c")) is None


@pytest.mark.parametrize("form", [win, msys, str])
def test_write_over_existing_file_fixes_only_added_lines(mk, home, form):
    f = home / "code" / "proj" / "old.md"
    f.write_text(f"title\nold {EM} line\n", encoding="utf-8")
    keep = f"title\nold {EM} line\nnew clean line\n"
    assert content_scan.handle(mk("Write", file_path=form(f), content=keep)) is None
    moved = f"old {EM} line\ntitle\n"
    assert content_scan.handle(mk("Write", file_path=form(f), content=moved)) is None
    added = f"title\nold {EM} line\nanother {EN} one\n"
    want = f"title\nold {EM} line\nanother; one\n"
    assert fixed(content_scan.handle(mk("Write", file_path=form(f), content=added))) == want
    edited = f"title\nold {EM} line changed\n"
    assert fixed(content_scan.handle(mk("Write", file_path=form(f), content=edited))) == "title\nold; line changed\n"


def test_write_relative_path_resolves_against_cwd(mk, home):
    f = home / "code" / "proj" / "old.md"
    f.write_text(f"old {EM} line\n", encoding="utf-8")
    assert content_scan.handle(mk("Write", file_path="old.md", content=f"old {EM} line\nok\n")) is None
    assert fixed(content_scan.handle(mk("Write", file_path="new.md", content=f"old {EM} line\n"))) == "old; line\n"


def test_edit_preserves_lines_already_in_old_string(mk, home):
    """Per Edit il confronto è con `old_string`, non con il file su disco."""
    f = home / "code" / "proj" / "old.md"
    f.write_text(f"old {EM} line\n", encoding="utf-8")
    same = mk("Edit", file_path=win(f), old_string=f"old {EM} line", new_string=f"old {EM} line\nadded")
    assert content_scan.handle(same) is None                     # riga grandfathered, invariata
    grown = mk("Edit", file_path=win(f), old_string=f"old {EM} line", new_string=f"old {EM} line\nnew {EM} one")
    assert fixed(content_scan.handle(grown), "new_string") == f"old {EM} line\nnew; one"
    assert fixed(content_scan.handle(mk("Edit", file_path=win(f), new_string=f"old {EM} line")), "new_string") == "old; line"


def test_firewall_still_blocks_after_a_dash_rewrite(mk, home):
    ctx = mk("Write", file_path=win(home / "code" / "proj" / "README.md"), content=f"see ~/.claude/rules {EM} internal")
    res = content_scan.handle(ctx)
    assert denied(res) and "firewall" in res.block


FORBIDDEN = [
    "see ~/.claude/rules/00.md", r"see ~\.claude\hooks", "in .claude/rules we say", "the agent-memory dir",
    "shared/projects/foo.md", "class/meta", "class/meta/x.md", "per MEMORY.md", "mtm.md", "LTM.md", ".memory.db",
    "memory_db.py query", "Meta says so", "memory.md says", "According to the gotchas file",
    "see the project memory", "per GM-3 and G-27", "cf. MT-4", "(CW-9)",
    # id a una lettera: bloccano solo con un contesto di memoria sulla stessa riga
    "gotcha M-12 applies", "| W-2 | fix the loader |", "|M-4|", "Occ=2, see G-27", "lesson W-3",
    "a Mistake: M-9", "superclaude M-1", "the memory cell G-5", "good-idea W-8", "3 occurrences of M-2",
]


@pytest.mark.parametrize("snippet", FORBIDDEN)
@pytest.mark.parametrize("form", [win, msys])
def test_firewall_blocks_internal_refs_in_project_files(mk, home, snippet, form):
    ctx = mk("Write", file_path=form(home / "code" / "proj" / "README.md"), content=f"intro\n{snippet}\n")
    res = content_scan.handle(ctx)
    assert denied(res) and "firewall" in res.block


def test_firewall_absolute_claude_home(mk, home):
    target = win(home / "code" / "proj" / "run.ps1")
    assert denied(content_scan.handle(mk("Write", file_path=target, content=f"cat {win(home / '.claude' / 'x.md')}")))
    assert denied(content_scan.handle(mk("Write", file_path=target, content=f"cat {msys(home / '.claude' / 'x.md')}")))


@pytest.mark.parametrize("role", ["plain", "meta", "worker"])
def test_firewall_applies_to_every_role_and_relative_paths(mk, role):
    assert denied(content_scan.handle(mk("Edit", role=role, file_path="docs/a.md", new_string="agent-memory")))


def test_firewall_scope(mk, home):
    text = "see ~/.claude/rules and M-12"
    assert content_scan.handle(mk("Write", file_path=win(home / ".claude" / "rules" / "x.md"), content=text)) is None
    assert content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / "x.md"), content=text)) is None
    assert content_scan.handle(mk("Agent", prompt=text)) is None


@pytest.mark.parametrize("text", [
    "removed subsystems: .comms.db .broker.db comms_db.py", "section C4 and B-tree", "HTTP-200 and X-1",
    "project-local .claude/settings.json docs", "the summary.md file",
    "src/subclass/metadata.py", "see class/metadata", "project_memory.md and xltm.md", "my-agent-memory_x",
])
def test_firewall_clean_text_passes(mk, home, text):
    assert content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "a.md"), content=text)) is None


@pytest.mark.parametrize("text,ids", [
    ("fixes M-12", ["M-12"]), ("Form W-2 and G-7 summit", ["W-2", "G-7"]), ("the M-16 rifle", ["M-16"]),
    ("TLS-M-1 handshake", ["M-1"]), ("M-1\nmemory is elsewhere", ["M-1"]),   # il contesto vale solo sulla stessa riga
    ("| W-2 form | tax |", ["W-2"]),                                          # non e' una cella da sola
])
def test_single_letter_ids_without_context_only_warn(mk, home, text, ids):
    res = content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "a.md"), content=text))
    assert res is not None and not res.block and res.updated_input is None
    assert len(res.context) == 1 and "memory cell id" in res.context[0]
    for i in ids:
        assert f"'{i}'" in res.context[0]


def test_suspect_ids_are_deduplicated_and_capped(mk, home):
    text = "W-1 W-1 W-2 W-3 W-4 W-5 W-6 W-7"
    res = content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "a.md"), content=text))
    note = res.context[0]
    assert note.count("'W-1'") == 1 and "'W-5'" in note and "'W-6'" not in note and ", ..." in note


def test_dash_rewrite_and_id_warning_share_one_result(mk, home):
    ctx = mk("Write", file_path=win(home / "code" / "proj" / "a.md"), content=f"Form W-2 {EM} filed")
    res = content_scan.handle(ctx)
    assert not res.block and res.updated_input["content"] == "Form W-2; filed"
    assert len(res.context) == 2 and "replaced 1" in res.context[0] and "'W-2'" in res.context[1]


def test_single_letter_ids_outside_code_are_ignored(mk, home):
    assert content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / "a.md"), content="fixes M-12")) is None


@pytest.mark.parametrize("ti", [
    {}, {"content": None}, {"content": 42, "file_path": 7}, {"edits": 5}, {"edits": [None, 3, {"new_string": None}]},
    {"prompt": ["x"]}, {"file_path": None, "content": "fine"},
])
@pytest.mark.parametrize("tool", ["Write", "MultiEdit", "Agent"])
def test_malformed_input_never_raises(mk, tool, ti):
    assert content_scan.handle(mk(tool, tool_input=ti)) is None


def test_non_dict_tool_input(mk):
    assert content_scan.handle(mk("Write", tool_input=None)) is None


# --------------------------------------------------------------------------
# Messaggi di commit scritti nella shell
# --------------------------------------------------------------------------
def shell_fixed(res):
    """Comando corretto restituito dalla guardia, oppure None."""
    return fixed(res, "command")


def test_registered_for_write_and_shell_tools():
    from sc_hooks import core
    assert any(n == "content-scan" and e == "PreToolUse" and set(t) == set(core.WRITE_TOOLS + core.SHELL_TOOLS)
               for e, t, n, _ in core._REGISTRY)


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_commit_message_dash_is_rewritten(mk, tool):
    res = content_scan.handle(mk(tool, command=f'git commit -m "fix: a {EM} b"', description="d"))
    assert shell_fixed(res) == 'git commit -m "fix: a; b"'
    assert res.updated_input["description"] == "d"              # input COMPLETO
    assert res.context and "replaced 1" in res.context[0] and "commit message" in res.context[0]


def test_commit_heredoc_form_is_rewritten_line_by_line(mk):
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        f"feat(x): add thing {EM} fast\n"
        "\n"
        f"Range 10{EN}20 covered; don't stop.\n"
        "Plain line - with hyphen.\n"
        "\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "EOF\n"
        ")\""
    )
    want = cmd.replace(f" {EM} ", "; ").replace(f"10{EN}20", "10-20")
    res = content_scan.handle(mk("Bash", command=cmd))
    assert shell_fixed(res) == want
    assert "replaced 2" in res.context[0]


@pytest.mark.parametrize("cmd,want", [
    (f'git -C "C:/Users/x/code/proj" commit -m "a {EM} b"', 'git -C "C:/Users/x/code/proj" commit -m "a; b"'),
    (f'git commit -m "subject" -m "body {EN} more"', 'git commit -m "subject" -m "body; more"'),
    (f'cd C:/code/proj && git add src/a.py && git commit -m "a {EM} b"', 'cd C:/code/proj && git add src/a.py && git commit -m "a; b"'),
    (f'git commit -m "use python and grep {EM} faster"', 'git commit -m "use python and grep; faster"'),
    (f"bash -c \"git commit -m 'x {EM} y'\"", "bash -c \"git commit -m 'x; y'\""),
])
def test_commit_variants_are_rewritten(mk, cmd, want):
    assert shell_fixed(content_scan.handle(mk("Bash", command=cmd))) == want


def test_powershell_here_string_keeps_crlf(mk):
    cmd = f"git -C C:/code/proj commit -m @'\r\nsubject {EM} x\r\n\r\nbody 1{EN}2\r\n'@"
    want = "git -C C:/code/proj commit -m @'\r\nsubject; x\r\n\r\nbody 1-2\r\n'@"
    assert shell_fixed(content_scan.handle(mk("PowerShell", command=cmd))) == want


@pytest.mark.parametrize("tool,cmd,runs", [
    ("Bash", f'git commit -m "a {EM} b" && git log | grep foo', "grep"),
    ("Bash", f"git commit -am \"a {EM} b\"; sed -i 's/x/y/' f.txt", "sed"),
    ("Bash", f'python fix.py && git commit -m "a {EN} b"', "python"),
    ("PowerShell", f"$m = 'a {EM} b' -replace 'x','y'; git commit -m $m", "-replace"),
    ("PowerShell", f'git commit -m "a {EM} b"; Get-Content f | Select-String "z"', "Select-String"),
])
def test_commit_with_dash_sensitive_tool_only_notes(mk, tool, cmd, runs):
    res = content_scan.handle(mk(tool, command=cmd))
    assert res is not None and not res.block and res.updated_input is None
    assert f"also runs {runs}" in res.context[0] and "reword" in res.context[0]


@pytest.mark.parametrize("cmd", [
    f'echo "a {EM} b" > notes.txt',
    f'git log --grep "a {EM} b"',
    f'git commit-tree HEAD^{{tree}} -m "a {EM} b"',
    'git commit -m "plain message - with hyphen"',
    "git status",
])
def test_non_commit_or_dashless_commands_are_untouched(mk, cmd):
    assert content_scan.handle(mk("Bash", command=cmd)) is None


def test_firewall_does_not_apply_to_shell_commands(mk):
    res = content_scan.handle(mk("Bash", command=f'git commit -m "see ~/.claude/rules and M-12 {EM} x"'))
    assert not res.block and shell_fixed(res) == 'git commit -m "see ~/.claude/rules and M-12; x"'
    assert len(res.context) == 1


@pytest.mark.parametrize("ti", [{}, {"command": None}, {"command": 42}, {"command": ["git", "commit"]}])
def test_malformed_shell_input_never_raises(mk, ti):
    assert content_scan.handle(mk("Bash", tool_input=ti)) is None
    assert content_scan.handle(mk("PowerShell", tool_input=None)) is None


def test_source_files_contain_no_forbidden_dash():
    from pathlib import Path
    root = Path(content_scan.__file__).resolve().parent
    mine = ["_util", "write_acl", "content_scan", "git_policy", "commit_gate", "git_verb",
            "worker_verify", "notebook_guard"]
    for name in mine:
        text = (root / f"{name}.py").read_text(encoding="utf-8")
        assert EM not in text and EN not in text, name
