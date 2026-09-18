"""Test della guardia notebook-guard: scritture su .ipynb solo tramite la skill /notebook."""
from __future__ import annotations

import pytest
from conftest import msys, win

from sc_hooks import core
from sc_hooks.handlers import notebook_guard

SHELLS = ("Bash", "PowerShell")


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
    "import json\nnb = json.load(open('a.ipynb'))", "p = 'x.ipynb'\njson.dump(nb, fh)",
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


BLOCKED_SHELL = [
    "jupyter nbconvert --to notebook --execute --inplace a.ipynb",
    "jupyter nbconvert --execute a.ipynb --inplace",
    "cd nb && jupyter.exe nbconvert --inplace b.ipynb",
    "python -c \"import json; json.dump(nb, open('a.ipynb','w'))\"",
    "python3 -c \"import json; d=json.load(open('a.ipynb'))\"",
    "python -c \"import nbformat; nbformat.write(nb, open('a.ipynb','w'))\"",
    "py -3 -c \"import json; json.load(open('a.ipynb'))\"",
    "& C:\\Python312\\python.exe -c \"import json; json.load(open('a.ipynb'))\"",
    "\"C:/Users/x/.venv/Scripts/python.exe\" -c \"import json; json.dump(n, open('a.ipynb','w'))\"",
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


@pytest.mark.parametrize("tool", ["Write", "Edit", "Bash", "PowerShell"])
@pytest.mark.parametrize("ti", [{}, None, "x", {"file_path": 5, "command": 5}, {"file_path": None, "content": None},
                                {"file_path": "a.py", "edits": 3}])
def test_malformed_input(mk, tool, ti):
    assert notebook_guard.handle(mk(tool, tool_input=ti)) is None


def test_registration_covers_all_tools():
    (tools,) = [set(t) for e, t, n, _ in core._REGISTRY if n == "notebook-guard" and e == "PreToolUse"]
    assert tools == {"NotebookEdit", "Write", "Edit", "MultiEdit", "Bash", "PowerShell"}
