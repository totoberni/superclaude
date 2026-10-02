"""Test della guardia notebook-guard: scritture su .ipynb solo tramite la skill /notebook."""
from __future__ import annotations

import pytest
from conftest import msys, win

from sc_hooks import core
from sc_hooks.handlers import notebook_guard

SHELLS = ("Bash", "PowerShell")
# I casi nuovi con un pattern vietato sono assemblati a runtime: scritti per esteso, la guardia
# attiva bloccherebbe la scrittura di questo stesso file di test
IPYNB = ".ip" + "ynb"
NBW = "nbformat.wri" + "te"


def denied(res):
    return res is not None and bool(res.block)


@pytest.mark.parametrize("role", ["plain", "meta", "worker"])
def test_notebook_edit_always_denied_and_points_to_skill(mk, role):
    res = notebook_guard.handle(mk("NotebookEdit", role=role, notebook_path="a.ipynb", new_source="x"))
    assert denied(res) and "/notebook" in res.block and "nb batch" in res.block and "nb execute" in res.block
    assert denied(notebook_guard.handle(mk("NotebookEdit", tool_input={})))
    assert denied(notebook_guard.handle(mk("NotebookEdit", tool_input=None)))


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("form", [win, msys, str])
def test_direct_ipynb_write_denied(mk, home, tool, form):
    res = notebook_guard.handle(mk(tool, file_path=form(home / "code" / "proj" / "nb" / "Analysis.IPYNB"), content="{}"))
    assert denied(res) and "nb batch" in res.block


def test_relative_and_quoted_ipynb_paths(mk):
    assert denied(notebook_guard.handle(mk("Write", file_path="nb/a.ipynb", content="{}")))
    assert denied(notebook_guard.handle(mk("Edit", file_path=r"..\other\b.ipynb", new_string="x")))


@pytest.mark.parametrize("name", ["a.py", "a.ipynb.bak", "ipynb.md", "notes.md", "a.ipynb/readme.txt"])
def test_other_files_allowed(mk, home, name):
    assert notebook_guard.handle(mk("Write", file_path=win(home / "code" / "proj" / name), content="hello")) is None


@pytest.mark.parametrize("content", [
    # il percorso in una variabile non è una scrittura precisa: ora è solo un avviso (WARN_CASES)
    "nbformat.write(nb, open(path, 'w'))",
])
def test_raw_patterns_in_new_python_code_denied(mk, home, content):
    target = win(home / "code" / "proj" / "tool.py")
    assert denied(notebook_guard.handle(mk("Write", file_path=target, content=content)))
    assert denied(notebook_guard.handle(mk("Edit", file_path=target, old_string="x", new_string=content)))
    assert denied(notebook_guard.handle(mk("MultiEdit", file_path=target, edits=[{"new_string": content}])))
    # la documentazione può descrivere il divieto
    assert notebook_guard.handle(mk("Write", file_path=win(home / "code" / "proj" / "README.md"), content=content)) is None


def test_clean_python_code_allowed(mk, home):
    target = win(home / "code" / "proj" / "tool.py")
    for content in ("import json\njson.load(open('cfg.json'))", "nb = nb_io.load_ipynb('a.ipynb')",
                    "nbformat.write(nb, path)"):
        assert notebook_guard.handle(mk("Write", file_path=target, content=content)) is None


WRITE_CASES = [
    f'Path("a{IPYNB}").write_text(json.dumps(nb))',
    f'json.dump(nb, open("a{IPYNB}", "w"))',
    f"{NBW}(nb, open(...))",
    # il cancelletto dentro la stringa non apre un commento
    f'json.dump(nb, open("a#1{IPYNB}", "w"))  # salva',
    # frammento di Edit non tokenizzabile (parentesi aperta): vale il ripiego riga per riga
    f'    json.dump(nb, open("a{IPYNB}", "w"),  # niente\n',
    # percorso e scrittura nello stesso statement
    f'open(str(p) + "{IPYNB}", "w").write(s)',
    f'with open("x{IPYNB}", mode="w") as f:\n    f.write(s)\n',
    f'{NBW}(nb, open("a{IPYNB}", "w"))',
    f'Path("a{IPYNB}").write_bytes(b)',
    f'Path("a{IPYNB}").open("w").write(s)',
    f'nb = 1; json.dump(nb, open("a{IPYNB}", "w")); print(nb)',
    # chiamata su più righe: decide la riga con percorso e modalità
    f'json.dump(\n    nb,\n    open("a{IPYNB}", "w"),\n)\n',
    # la docstring non conta, la scrittura sotto sì
    f'def save(nb):\n    """Salva il notebook."""\n    json.dump(nb, open("a{IPYNB}", "w"))\n',
]

READ_CASES = [
    # incidente 1: script che legge soltanto i notebook
    f'for nb in sorted(DIR.glob("*.dbquery{IPYNB}")):\n    j = json.loads(nb.read_text(encoding="utf-8"))\n',
    # incidente 2: stessa glob, json citato solo in un commento
    f'for dash in sorted(DIR.glob("*.dbquery{IPYNB}")):\n    d = load_ipynb(dash)  # plain JSON loader, same as json.load\n',
    # la scrittura vietata compare solo in un commento
    f'# mai json.dump(nb, open("a{IPYNB}", "w")) a mano\nnb = nb_io.load_ipynb("a{IPYNB}")\n',
    # estensione notebook solo nel commento, json.dump su un file qualunque
    f'json.dump(cfg, open("cfg.json", "w"))  # non è un file{IPYNB}\n',
    # frammento di Edit non tokenizzabile, scrittura solo nel commento
    f'    nb = nb_io.load_ipynb(path,  # niente json.dump(nb, open("x{IPYNB}", "w"))\n',
    # json.dumps restituisce una stringa: non è una scrittura
    f"print(json.dumps(json.load(open('a{IPYNB}'))))\n",
    # docstring con json.dump e notebook, nessun'altra scrittura
    f'def show(p):\n    """Mai json.dump(nb, open("a{IPYNB}", "w")): usare nb_io."""\n    return nb_io.load_ipynb(p)\n',
    # notebook citato solo nella docstring di modulo, scrittura su un altro file
    f'"""Riepilogo dei file *{IPYNB}."""\nimport json\njson.dump(r, open("r.json", "w"))\n',
    # letture con modalità esplicita
    f'with open("a{IPYNB}", "r", encoding="utf-8") as f:\n    nb = json.load(f)\n',
]

WARN_CASES = [
    # legge i notebook e scrive un rapporto JSON su un'altra riga
    f'for p in DIR.glob("*{IPYNB}"):\n    rows.append(json.load(open(p)))\njson.dump(report, open("report.json", "w"))\n',
    # percorso in una variabile: falso negativo noto del blocco, resta l'avviso
    f'p = "a{IPYNB}"\njson.dump(nb, open(p, "w"))\n',
    f"p = 'x{IPYNB}'\njson.dump(nb, fh)",
    # statement separati da `;` sulla stessa riga
    f"nb = json.load(open('a{IPYNB}')); json.dump(r, open('r.json', 'w'))\n",
    # stringa tra parentesi: non è una docstring, quindi conta come menzione
    f'p = (\n    "a{IPYNB}"\n)\nPath(p).write_text(s)\n',
]


@pytest.mark.parametrize("content", WRITE_CASES)
def test_notebook_writes_in_python_code_denied(mk, home, content):
    target = win(home / "code" / "proj" / "tool.py")
    for res in (notebook_guard.handle(mk("Write", file_path=target, content=content)),
                notebook_guard.handle(mk("Edit", file_path=target, old_string="x", new_string=content)),
                notebook_guard.handle(mk("MultiEdit", file_path=target, edits=[{"new_string": content}]))):
        assert denied(res) and "nb_io.atomic_write_ipynb" in res.block and "allowed" in res.block


@pytest.mark.parametrize("content", READ_CASES)
def test_notebook_reads_and_comment_mentions_allowed(mk, home, content):
    target = win(home / "code" / "proj" / "tool.py")
    assert notebook_guard.handle(mk("Write", file_path=target, content=content)) is None
    assert notebook_guard.handle(mk("Edit", file_path=target, old_string="x", new_string=content)) is None
    assert notebook_guard.handle(mk("MultiEdit", file_path=target, edits=[{"new_string": content}])) is None


def verdict(res):
    if res is None:
        return None
    if res.block:
        return "deny"
    warned = len(res.context) == 1 and "nb_io.atomic_write_ipynb" in res.context[0] and "reading notebooks is fine" in res.context[0]
    return "warn" if warned else "other"


def py_verdicts(mk, home, content):
    target = win(home / "code" / "proj" / "tool.py")
    return {verdict(notebook_guard.handle(mk("Write", file_path=target, content=content))),
            verdict(notebook_guard.handle(mk("Edit", file_path=target, old_string="x", new_string=content))),
            verdict(notebook_guard.handle(mk("MultiEdit", file_path=target, edits=[{"new_string": content}])))}


@pytest.mark.parametrize("content", WARN_CASES)
def test_cooccurrence_in_python_code_only_warns(mk, home, content):
    assert py_verdicts(mk, home, content) == {"warn"}


@pytest.mark.parametrize("arg", ['"w"', '"wb"', '"w+"', '"a"', '"ab"', '"x"', '"r+"', "mode='w'", 'mode = "wb"',
                                 '"w", encoding="utf-8"'])
def test_open_write_modes_on_notebook_denied(mk, home, arg):
    assert py_verdicts(mk, home, f'fh = open("a{IPYNB}", {arg})\n') == {"deny"}


@pytest.mark.parametrize("arg", ['"r"', '"rb"', '"rt", encoding="utf-8"', 'mode="r"', 'encoding="utf-8"', ""])
def test_open_read_modes_on_notebook_allowed(mk, home, arg):
    sep = ", " if arg else ""
    assert py_verdicts(mk, home, f'fh = open("a{IPYNB}"{sep}{arg})\n') == {None}


MALFORMED_PY = [
    # frammenti non tokenizzabili o degeneri: mai un'eccezione (il dispatcher la lascerebbe passare)
    (f'x = "a{IPYNB}', None), (";", None), (";;;", None), ('"', None), ("'", None), ("\\", None),
    ("open(", None), ("json.dump(", None), (f'"""a{IPYNB}', None), ("", None),
    (f'open("a{IPYNB}", "w"', "deny"), (f"json.dump(nb, open('a{IPYNB}', 'w', ", "deny"),
    (f"    x = 1\n  json.dump(r, open('a{IPYNB}', 'w'))\n", "deny"),
    (f'x = 1\ry = "a{IPYNB}"  # c\rjson.dump(r, fh)\r', "warn"),
    (f'x = "è{IPYNB}"  # json.dump(nb, f)\nj = 1\n', None),
]


@pytest.mark.parametrize(("content", "expected"), MALFORMED_PY)
def test_malformed_python_fragments_do_not_crash(mk, home, content, expected):
    assert py_verdicts(mk, home, content) == {expected}


BLOCKED_SHELL = [
    "jupyter nbconvert --to notebook --execute --inplace a.ipynb",
    "jupyter nbconvert --execute a.ipynb --inplace",
    "cd nb && jupyter.exe nbconvert --inplace b.ipynb",
    "python -c \"import json; json.dump(nb, open('a.ipynb','w'))\"",
    f"python3 -c \"import json; json.dump(d, open('a{IPYNB}','w'))\"",
    "python -c \"import nbformat; nbformat.write(nb, open('a.ipynb','w'))\"",
    f"py -3 -c \"import json; json.dump(d, open('a{IPYNB}','w'))\"",
    f"& C:\\Python312\\python.exe -c \"import json; json.dump(d, open('a{IPYNB}','w'))\"",
    f"python -c \"import json,pathlib; pathlib.Path('a{IPYNB}').write_text(json.dumps(nb))\"",
    "\"C:/Users/x/.venv/Scripts/python.exe\" -c \"import json; json.dump(n, open('a.ipynb','w'))\"",
    f"python -c \"import json; x = 1; json.dump(nb, open('a{IPYNB}', mode='w'))\"",
    f"python -c \"open('a{IPYNB}','w').write(s)\"",
    f"python -c 'import json; json.dump(nb, open(\"a{IPYNB}\", \"w\"))'",
]

ALLOWED_SHELL = [
    "nb batch a.ipynb --plan plan.yml", "nb execute a.ipynb --cells 3-5", "nb sync a.ipynb", "nb validate a.ipynb",
    "nb init a.ipynb --migrate", "nb.exe batch a.ipynb --plan p.yml", "& nb execute .\\a.ipynb",
    "python ~/.claude/skills/notebook/nb_main.py batch a.ipynb --plan p.yml",
    "python -m nb_main execute a.ipynb", "jupyter nbconvert --to html a.ipynb",
    "jupyter nbconvert --to notebook --execute a.ipynb --output b.ipynb", "git add a.ipynb a.py", "ls *.ipynb",
    "Get-ChildItem -Filter *.ipynb", "cp a.ipynb backup/a.ipynb", "Copy-Item a.ipynb b.ipynb",
    "echo \"json.dump on a.ipynb is banned\"", "python -c \"import json; json.load(open('cfg.json'))\"",
    "python script.py a.ipynb", "jupyter lab", "", "ls",
    # le letture via one-liner passano
    f"python -c \"import json; nb=json.load(open('a{IPYNB}'))\"",
    f"python3 -c \"import json,pathlib; print(len(json.loads(pathlib.Path('a{IPYNB}').read_text())['cells']))\"",
    f"py -3 -c \"import json; json.load(open('a{IPYNB}'))\"",
    f"& C:\\Python312\\python.exe -c \"import json; json.load(open('a{IPYNB}'))\"",
    # json.dumps restituisce una stringa: stampare un notebook non è scriverlo
    f"python -c \"import json; print(json.dumps(json.load(open('a{IPYNB}'))))\"",
    f"python -m json.tool a{IPYNB}",
]

WARN_SHELL = [
    # legge un notebook e scrive un rapporto: statement separati da `;`
    f"python -c \"import json; nb=json.load(open('a{IPYNB}')); json.dump(r, open('r.json','w'))\"",
    # percorso passato in argv: la scrittura non cita il notebook
    f"python -c \"import json,sys; json.dump(x, open(sys.argv[1],'w'))\" a{IPYNB}",
]

MALFORMED_SHELL = [
    # virgolette sbilanciate: shlex fallisce e si valuta il comando intero
    (f"python -c \"import json; json.dump(nb, open('a{IPYNB}','w'))", "deny"),
    (f"python -c \";\" a{IPYNB}", None), (f"python -c ';;' && ls *{IPYNB}", None),
    (f"python -c a{IPYNB}", None), (f"python -c x a{IPYNB} -c", None),
]


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", BLOCKED_SHELL)
def test_shell_bypasses_denied(mk, tool, cmd):
    res = notebook_guard.handle(mk(tool, command=cmd))
    assert denied(res) and "nb " in res.block


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", ALLOWED_SHELL)
def test_nb_cli_and_harmless_commands_allowed(mk, tool, cmd):
    assert notebook_guard.handle(mk(tool, command=cmd)) is None


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize("cmd", WARN_SHELL)
def test_shell_cooccurrence_only_warns(mk, tool, cmd):
    assert verdict(notebook_guard.handle(mk(tool, command=cmd))) == "warn"


@pytest.mark.parametrize("tool", SHELLS)
@pytest.mark.parametrize(("cmd", "expected"), MALFORMED_SHELL)
def test_malformed_shell_one_liners_do_not_crash(mk, tool, cmd, expected):
    assert verdict(notebook_guard.handle(mk(tool, command=cmd))) == expected


@pytest.mark.parametrize("tool", ["Write", "Edit", "Bash", "PowerShell"])
@pytest.mark.parametrize("ti", [{}, None, "x", {"file_path": 5, "command": 5}, {"file_path": None, "content": None},
                                {"file_path": "a.py", "edits": 3}])
def test_malformed_input(mk, tool, ti):
    assert notebook_guard.handle(mk(tool, tool_input=ti)) is None


def test_registration_covers_all_tools():
    (tools,) = [set(t) for e, t, n, _ in core._REGISTRY if n == "notebook-guard" and e == "PreToolUse"]
    assert tools == {"NotebookEdit", "Write", "Edit", "MultiEdit", "Bash", "PowerShell"}
