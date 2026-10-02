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
    ("| Ricavi | {EM} |", "| Ricavi | - |"),     # cella di tabella vuota: resta un trattino
    ("|{EN}|{EM}|", "|-|-|"),
    ("| a {EM} b |", "| a; b |"),              # inciso dentro una cella: regola normale
    ("a {EM}{EM} b", "a; b"),                  # una serie di trattini lunghi vale come uno
    ("a{EM}{EM}b", "a; b"),
    ("a {EM} {EM} b", "a; b"),
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
    # anche i plurali del contesto
    "see gotchas M-12", "lessons W-3", "two mistakes: M-9", "memories M-2", "good-ideas W-8",
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


# --------------------------------------------------------------------------
# Codice e dati: nel codice si corregge solo il commento, il resto resta e avvisa
# --------------------------------------------------------------------------
def left_notes(res):
    """Note sui trattini lasciati nel codice o nei dati."""
    return [c for c in res.context if "untouched" in c]


PY_DATA_LINES = [
    f're.compile("[{EN}{EM}]")',                   # la regex cambierebbe significato
    f's.replace("{EM}", ", ")',
    f'assert normalize("a{EM}b") == "a, b"',
    f'label = "Totale {EM} 2025"',
    f'x = "a # b {EM} c"',                         # `#` dentro una stringa non apre un commento
]


@pytest.mark.parametrize("line", PY_DATA_LINES)
def test_python_code_dashes_stay_byte_identical_and_warn(mk, home, line):
    res = content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "m.py"), content=f"import re\n{line}\n"))
    assert res is not None and not res.block and res.updated_input is None
    assert len(res.context) == 1 and "(lines 2)" in res.context[0]
    assert "reword it" in res.context[0] and "keep it" in res.context[0]


def test_python_comments_are_rewritten(mk, home):
    src = f"# calcola il margine {EM} vedi nota\n    # rientrato {EN} ok\nx = 1  # nota {EM} vedi\n"
    res = content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "m.py"), content=src))
    assert fixed(res) == "# calcola il margine; vedi nota\n    # rientrato; ok\nx = 1  # nota; vedi\n"
    assert len(res.context) == 1 and "replaced 3" in res.context[0]


def test_python_file_mixes_rewrite_and_one_warning(mk, home):
    code = PY_DATA_LINES[:4]
    src = "\n".join([f"# calcola il margine {EM} vedi nota", *code, f"y = \"#\" + 'a{EM}b'  # nota {EM} c", ""])
    res = content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "m.py"), content=src))
    out = fixed(res).split("\n")
    assert out[0] == "# calcola il margine; vedi nota"
    assert out[1:5] == code                                       # codice identico byte per byte
    assert out[5] == f"y = \"#\" + 'a{EM}b'  # nota; c"           # solo la coda di commento
    assert len(res.context) == 2 and "replaced 2" in res.context[0]
    assert left_notes(res) == [res.context[1]]
    assert "left 6 " in res.context[1] and "(lines 2, 3, 4, 5, 6)" in res.context[1]


@pytest.mark.parametrize("name,src,want,left", [
    ("app.ts", f"const r = /[{EM}]/;  // separa {EM} qui", f"const r = /[{EM}]/;  // separa; qui", 1),
    ("app.ts", f'const u = "http://x {EM} y";', None, 1),          # `//` tra virgolette non è un commento
    ("app.js", f"/**\n * nota {EM} vedi\n */", "/**\n * nota; vedi\n */", 0),   # commento a blocco
    ("lib.c", f"*p = '{EM}';", None, 1),                           # `*` iniziale seguito da codice
    ("q.sql", f"SELECT 'a {EM} b' AS x -- nota {EM} vedi", f"SELECT 'a {EM} b' AS x -- nota; vedi", 1),
    ("init.lua", f"-- commento {EM} intero", "-- commento; intero", 0),
    ("run.sh", f'echo "a {EM} b"  # nota {EM} c', f'echo "a {EM} b"  # nota; c', 1),
    ("cfg.YAML", f'sep: "{EM}"', None, 1),                          # estensione senza distinzione di maiuscole
])
def test_comment_marker_by_extension(mk, home, name, src, want, left):
    res = content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / name), content=src))
    assert res is not None and not res.block
    assert fixed(res) == want
    notes = left_notes(res)
    assert len(notes) == (1 if left else 0)
    assert not left or f"left {left} " in notes[0]


@pytest.mark.parametrize("name", ["data.json", "rows.csv", "rows.tsv", "x.XML", "log.jsonl", "nb.ipynb"])
def test_data_files_are_never_rewritten(mk, home, name):
    res = content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / name), content=f'{{"sep": "{EM}", "r": "1{EN}2"}}\n'))
    assert res is not None and not res.block and res.updated_input is None
    assert len(res.context) == 1 and "left 2 em/en dash(es) in code or data untouched (lines 1)" in res.context[0]


def test_markdown_fenced_block_is_left_as_code(mk, home):
    src = f"intro {EM} testo\n```python\nsep = '{EM}'\n```\nfine {EM} testo\n"
    res = content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / "README.md"), content=src))
    assert fixed(res) == f"intro; testo\n```python\nsep = '{EM}'\n```\nfine; testo\n"
    assert len(res.context) == 2 and "replaced 2" in res.context[0] and "(lines 3)" in res.context[1]


def test_markdown_fence_closes_only_on_matching_fence(mk, home):
    src = f"~~~~\n```\nin {EM}\n```\n~~~~\nout {EM}"           # il ``` interno non chiude il ~~~~
    res = content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / "a.md"), content=src))
    assert fixed(res) == f"~~~~\n```\nin {EM}\n```\n~~~~\nout;"
    assert "(lines 3)" in left_notes(res)[0]


def test_left_note_caps_line_numbers(mk, home):
    src = "\n".join(f'v{i} = "{EM}"' for i in range(7))
    note = content_scan.handle(mk("Write", file_path=win(home / "elsewhere" / "m.py"), content=src)).context[0]
    assert "left 7 " in note and "(lines 1, 2, 3, 4, 5, ...)" in note


def test_grandfathered_code_lines_do_not_warn(mk, home):
    f = home / "code" / "proj" / "m.py"
    f.write_text(f'SEP = "{EM}"\n', encoding="utf-8")
    assert content_scan.handle(mk("Write", file_path=win(f), content=f'SEP = "{EM}"\nOTHER = 1\n')) is None
    same = mk("Edit", file_path=win(f), old_string=f'SEP = "{EM}"', new_string=f'SEP = "{EM}"\nOTHER = 1')
    assert content_scan.handle(same) is None
    grown = mk("Edit", file_path=win(f), old_string=f'SEP = "{EM}"', new_string=f'SEP = "{EM}"\nNEW = "{EN}"  # a {EM} b')
    res = content_scan.handle(grown)
    assert fixed(res, "new_string") == f'SEP = "{EM}"\nNEW = "{EN}"  # a; b'
    assert len(left_notes(res)) == 1 and "left 1 " in left_notes(res)[0] and "(lines 2)" in left_notes(res)[0]


def test_code_rewrite_warning_and_id_warning_share_one_result(mk, home):
    src = f'FORM = "W-2 {EM}"  # vedi W-2 {EM} ok'
    res = content_scan.handle(mk("Write", file_path=win(home / "code" / "proj" / "tax.py"), content=src))
    assert not res.block and res.updated_input["content"] == f'FORM = "W-2 {EM}"  # vedi W-2; ok'
    assert len(res.context) == 3
    assert "replaced 1" in res.context[0] and "left 1 " in res.context[1] and "'W-2'" in res.context[2]


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


# --------------------------------------------------------------------------
# Si riscrive solo se ogni trattino sta in un testo: virgolette, heredoc, here-string
# --------------------------------------------------------------------------
CC_HEREDOC = (
    "git commit -m \"$(cat <<'EOF'\n"
    f"feat(x): say \"quoted\" {EM} don't split\n"
    "\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\n"
    "EOF\n"
    ")\""
)

QUOTED_COMMITS = [
    ("Bash", f'git commit -m "a {EM} b"', 'git commit -m "a; b"'),
    ("Bash", f"git commit -m 'a {EM} b'", "git commit -m 'a; b'"),
    ("PowerShell", f'git commit -m "a {EM} b"', 'git commit -m "a; b"'),
    ("Bash", CC_HEREDOC, CC_HEREDOC.replace(f" {EM} ", "; ")),
    ("Bash", f"git commit -F - <<-EOF\n\tsubject {EN} 10{EN}20\n\tEOF\n", "git commit -F - <<-EOF\n\tsubject; 10-20\n\tEOF\n"),
    ("Bash", f"git commit -F - << \"END\"\nbody {EM} x $(not run)\nEND", "git commit -F - << \"END\"\nbody; x $(not run)\nEND"),
    ("PowerShell", f"git commit -m @'\nsubject {EM} x, don't \"stop\"\n'@", "git commit -m @'\nsubject; x, don't \"stop\"\n'@"),
    ("Bash", f"git commit -m \"a {EM} b\" -m 'c {EN} d'", "git commit -m \"a; b\" -m 'c; d'"),
    ("PowerShell", f"git commit -m 'it''s a {EM} b'", "git commit -m 'it''s a; b'"),       # '' = apice escapato
    ("PowerShell", f'git commit -m "say `"hi`" {EM} ok"', 'git commit -m "say `"hi`"; ok"'),
    ("Bash", f'git commit -m "say \\"hi\\" {EM} ok"', 'git commit -m "say \\"hi\\"; ok"'),
    ("Bash", f"git commit -m $'a {EM} b\\n'", "git commit -m $'a; b\\n'"),
    ("Bash", f"git commit -m \"a {EM} b\"  # don't", "git commit -m \"a; b\"  # don't"),  # apice nel commento
    ("PowerShell", f"pwsh -c \"git commit -m 'a {EM} b'\"", "pwsh -c \"git commit -m 'a; b'\""),
]


@pytest.mark.parametrize("tool,cmd,want", QUOTED_COMMITS)
def test_commit_dash_inside_text_is_rewritten(mk, tool, cmd, want):
    res = content_scan.handle(mk(tool, command=cmd))
    assert shell_fixed(res) == want
    assert len(res.context) == 1 and "replaced" in res.context[0]


UNQUOTED_DASH_COMMITS = [
    ("Bash", f"git commit -m fix{EM}thing"),
    ("PowerShell", f"git commit -m fix{EM}thing"),
    ("Bash", f'git commit -m "a" {EM} b'),                        # dopo una virgoletta chiusa
    ("PowerShell", f"git commit -m 'a' {EN} b"),
    ("Bash", f'git commit -m "a {EM} b'),                         # virgolette non chiuse
    ("PowerShell", f"git commit -m 'a {EM} b"),
    ("Bash", f'echo a{EM}b && git commit -m "x"'),                # segmento senza virgolette
    ("PowerShell", f'echo a{EM}b; git commit -m "x"'),
    ("Bash", f'git commit -m "$(echo a{EM}b)"'),                  # sostituzione di comando: è codice
    ("Bash", f'git commit -m "`echo a{EM}b`"'),
    ("PowerShell", f'git commit -m "$(echo a{EM}b)"'),
    ("Bash", f"git commit -m a\\{EM}b"),                          # trattino escapato: resta codice
    ("Bash", f'git commit -m "x"  # nota {EM} y'),                # commento: non è testo
    ("Bash", f'git commit -m "a <<EOF"\necho x{EM}y\nEOF'),       # `<<` tra virgolette non apre un heredoc
    ("Bash", f"git commit -F - <<EOF\n$(echo a{EM}b)\nEOF\n"),     # corpo che espande `$(`
    ("Bash", f"git commit -F - <<EOF\nsubject {EM} x\n"),          # heredoc non chiuso
    ("PowerShell", f'git commit -m @"\n$(echo a{EM}b)\n"@'),       # here-string che espande `$(`
    ("PowerShell", f"git commit -m 'it{chr(0x2019)}s a {EM} b'"),  # apice tipografico: per PowerShell chiude
    ("Bash", f'bash -c "git commit -m x{EM}y"'),                  # la shell annidata vede il trattino fuori
    ("Bash", f'git commit -m "x" && echo "a{EM}b" | bash'),
    ("PowerShell", f"cmd /c \"git commit -m 'a {EM} b'\""),        # cmd.exe non modellato
]


@pytest.mark.parametrize("tool,cmd", UNQUOTED_DASH_COMMITS)
def test_commit_dash_outside_text_only_notes(mk, tool, cmd):
    res = content_scan.handle(mk(tool, command=cmd))
    assert res is not None and not res.block and res.updated_input is None
    assert len(res.context) == 1
    assert "outside quotes" in res.context[0] and "split the command" in res.context[0] and "reword" in res.context[0]


@pytest.mark.parametrize("tool,cmd", [
    ("Bash", f'git commit -m "a {EM} b" <<'),                     # `<<` in fondo
    ("Bash", f"git commit -m 'a {EM} b' <<'EOF"),                 # delimitatore non chiuso
    ("Bash", f'git commit -m "a {EM} b\\'),                       # escape in fondo, virgolette aperte
    ("Bash", f'git commit -m "$(cat <<EOF {EM}'),
    ("PowerShell", f"git commit -m @'\na {EM} b"),                 # `@'` senza chiusura
    ("PowerShell", f"git commit -m @'\n{EM}"),
    ("PowerShell", f"git commit -m 'a {EM} b' <# x"),
])
def test_malformed_commit_commands_note_without_raising(mk, tool, cmd):
    res = content_scan.handle(mk(tool, command=cmd))
    assert res is not None and not res.block and res.updated_input is None
    assert "outside quotes" in res.context[0]


def test_scanner_never_raises_on_token_soup():
    """Il dispatcher lascia passare se un handler solleva: lo scanner non deve mai sollevare."""
    import itertools
    toks = ['"', "'", "$(", ")", "<<", "<<-", "EOF", "\n", "`", "\\", "#", "@'", "'@", '@"', '"@', EM, " ", "$'", "<#"]
    for tool in ("Bash", "PowerShell"):
        for combo in itertools.product(toks, repeat=3):
            assert isinstance(content_scan._dashes_only_in_text("".join(combo) + EM, tool), bool)


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
