"""Guardia notebook-guard: tutte le scritture su .ipynb passano dalla skill /notebook
(ex modules/30-notebook-guard.sh). Stessa politica, blocco duro.

Bloccati:
- NotebookEdit, sempre;
- Write / Edit / MultiEdit su un file `.ipynb`;
- codice `.py` nuovo che SCRIVE un .ipynb con json grezzo (dump/dumps) o con
  `nbformat.write` su un `open()`; le letture passano e i commenti Python sono ignorati;
- shell: `jupyter nbconvert --inplace` su .ipynb, e one-liner `python -c` / `-m`
  con gli stessi pattern grezzi.
Tutto il resto passa, in particolare la CLI `nb` (`nb batch`, `nb execute`, ...).
"""
from __future__ import annotations

import io
import re
import tokenize

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "notebook-guard"

_SKILL = "Use the /notebook skill: `nb batch <nb> --plan <plan.yml>` for structural changes, `nb execute <nb>` to run cells"
_READS_OK = "Reading a notebook (json.load / json.loads) is allowed; only writes are blocked"
# Solo il lato scrittura conta: leggere un notebook con json.load non lo corrompe
_RAW_JSON = re.compile(r"json\.dumps?\s*\(")
_RAW_NBFORMAT = re.compile(r"nbformat\.write\([^)]*open\(")
_NBCONVERT = re.compile(r"jupyter(?:\.exe)?\s+nbconvert.*--inplace.*\.ipynb|jupyter(?:\.exe)?\s+nbconvert.*\.ipynb.*--inplace", re.I | re.S)
_PY_INLINE = re.compile(r"""(?:^|[\s;&|(/\\"'])(?:python[0-9.]*|py)(?:\.exe)?["']?\s+(?:-[A-Za-z0-9]+\s+)*?-[cm]\s""", re.I)
# Ripiego per frammenti non tokenizzabili: taglia la coda `#...` di ogni riga se il `#`
# non sta dentro una stringa a virgolette singole o doppie aperta e chiusa sulla stessa riga
_LINE_COMMENT = re.compile(r"""^((?:[^#'"\n]|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")*)#.*$""", re.M)


def _raw_pattern(text: str) -> str | None:
    if _RAW_NBFORMAT.search(text):
        return "raw nbformat.write(open(...))"
    if _RAW_JSON.search(text) and ".ipynb" in text.lower():
        return "raw json write on .ipynb"
    return None


def _strip_comments(src: str) -> str:
    """Toglie i commenti Python lasciando intatte le stringhe (dove vivono i percorsi .ipynb)."""
    lines = io.StringIO(src).readlines()
    try:
        cuts = {t.start[0]: t.start[1] for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type == tokenize.COMMENT}
    except (tokenize.TokenError, SyntaxError):
        # I frammenti di Edit spesso non tokenizzano (parentesi o indentazione monche)
        return _LINE_COMMENT.sub(r"\1", src)
    return "".join(line[:cuts[n]] if n in cuts else line for n, line in enumerate(lines, 1))


@core.on("PreToolUse", tools=core.WRITE_TOOLS + core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    if ctx.tool == "NotebookEdit":
        return core.Result.deny(f"NotebookEdit is disabled: every .ipynb write is atomic. {_SKILL}.")

    if ctx.tool in core.SHELL_TOOLS:
        cmd = _util.command_of(ctx)
        if ".ipynb" not in cmd.lower() and "nbformat" not in cmd:
            return None
        if _NBCONVERT.search(cmd):
            return core.Result.deny("`jupyter nbconvert --inplace` bypasses the atomic write and the lock. Use `nb execute <nb>`.")
        if _PY_INLINE.search(cmd):
            hit = _raw_pattern(cmd)
            if hit:
                return core.Result.deny(f"{hit} in a shell one-liner. {_SKILL}. {_READS_OK}.")
        return None

    path = _util.canon(_util.path_of(ctx), ctx.cwd if isinstance(ctx.cwd, str) else None)
    if path.endswith(".ipynb"):
        return core.Result.deny(
            f"direct {ctx.tool} on a .ipynb file is forbidden. {_SKILL}, or edit the paired .py and run `nb sync <nb>`."
        )
    if path.endswith(".py"):
        hit = _raw_pattern(_strip_comments(_util.new_text(ctx)))
        if hit:
            return core.Result.deny(
                f"{hit} in {path}. Use nb_io.load_ipynb / nb_io.atomic_write_ipynb from the /notebook skill. {_READS_OK}."
            )
    return None
