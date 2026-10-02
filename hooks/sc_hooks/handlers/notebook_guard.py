"""Guardia notebook-guard: tutte le scritture su .ipynb passano dalla skill /notebook
(ex modules/30-notebook-guard.sh). Si blocca solo ciò che è rischioso E riconosciuto con
precisione; il resto, se sospetto, riceve un avviso.

Bloccati:
- NotebookEdit, sempre;
- Write / Edit / MultiEdit su un file `.ipynb`;
- shell: `jupyter nbconvert --inplace` su .ipynb;
- codice `.py` nuovo e one-liner `python -c` / `-m` con una scrittura PRECISA su notebook:
  uno statement (riga fisica, spezzata anche sui `;` fuori dalle virgolette) che cita
  `.ipynb` e contiene un segnale di scrittura (json dump verso file, `nbformat.write`,
  `.write_text` / `.write_bytes`, `open` con una modalità di scrittura); inoltre
  `nbformat.write` con un `open` inline, che scrive un notebook per costruzione.
Solo avviso (Result.note): il codice scrive file e cita `.ipynb` in statement diversi, per
esempio con il percorso che passa da una variabile (falso negativo noto del blocco).
Commenti e docstring non contano; letture e `json.dumps` (restituisce una stringa) passano.
Tutto il resto passa, in particolare la CLI `nb` (`nb batch`, `nb execute`, ...).
"""
from __future__ import annotations

import io
import itertools
import re
import shlex
import tokenize
from collections.abc import Iterator

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "notebook-guard"

_SKILL = "Use the /notebook skill: `nb batch <nb> --plan <plan.yml>` for structural changes, `nb execute <nb>` to run cells"
_READS_OK = "Reading a notebook (json.load / json.loads) is allowed; only writes are blocked"
_WARN = ("notebook-guard: this code writes files and mentions .ipynb; if it writes a notebook, "
         "use nb_io.atomic_write_ipynb (or the nb CLI); reading notebooks is fine")
# Segnali di scrittura in forma di chiamata; json.dumps resta fuori: restituisce una stringa
_WRITE_CALL = re.compile(r"\bjson\.dump\s*\(|\bnbformat\.write\s*\(|\.write_(?:text|bytes)\s*\(")
_OPEN_CALL = re.compile(r"\bopen\s*\(")
# Argomento di open che chiede la scrittura: letterale di modalità, posizionale o come mode=
_WRITE_MODE = re.compile(r"""(?:mode\s*=\s*)?(['"])[rbtU]*[wax+][rwaxbtU+]*\1""")
_RAW_NBFORMAT = re.compile(r"nbformat\.write\([^)]*open\(")
_NBCONVERT = re.compile(r"jupyter(?:\.exe)?\s+nbconvert.*--inplace.*\.ipynb|jupyter(?:\.exe)?\s+nbconvert.*\.ipynb.*--inplace", re.I | re.S)
_PY_INLINE = re.compile(r"""(?:^|[\s;&|(/\\"'])(?:python[0-9.]*|py)(?:\.exe)?["']?\s+(?:-[A-Za-z0-9]+\s+)*?-[cm]\s""", re.I)
# Ripiego per frammenti non tokenizzabili: taglia la coda `#...` di ogni riga se il `#`
# non sta dentro una stringa a virgolette singole o doppie aperta e chiusa sulla stessa riga
_LINE_COMMENT = re.compile(r"""^((?:[^#'"\n]|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")*)#.*$""", re.M)
# Token che non aprono né chiudono una riga logica
_LAYOUT = {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}


def _outside_quotes(text: str) -> Iterator[tuple[int, str]]:
    """Coppie (indice, carattere) fuori dai letterali a virgolette singole o doppie."""
    quote, escaped = "", False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        else:
            yield i, ch


def _statements(text: str) -> list[str]:
    """Statement approssimati: righe fisiche, spezzate sui `;` fuori dalle virgolette."""
    out: list[str] = []
    for line in text.splitlines():
        cuts = [i for i, ch in _outside_quotes(line) if ch == ";"]
        out += [line[a + 1:b] for a, b in zip([-1, *cuts], [*cuts, len(line)])]
    return out


def _call_args(stmt: str, start: int) -> list[str]:
    """Argomenti di primo livello della chiamata la cui parentesi si apre subito prima di `start`."""
    seg = stmt[start:]
    args: list[str] = []
    depth = last = 0
    for i, ch in _outside_quotes(seg):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return [*args, seg[last:i]]
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(seg[last:i])
            last = i + 1
    # La chiamata prosegue oltre lo statement: si valuta quanto c'è
    return [*args, seg[last:]]


def _writes(stmt: str) -> bool:
    if _WRITE_CALL.search(stmt):
        return True
    return any(_WRITE_MODE.fullmatch(arg.strip())
               for m in _OPEN_CALL.finditer(stmt) for arg in _call_args(stmt, m.end()))


def _notebook_write(code: str) -> str | None:
    """Motivo del blocco se uno statement di `code` scrive un .ipynb in modo riconoscibile."""
    if _RAW_NBFORMAT.search(code):
        return "raw nbformat.write on an inline open()"
    if any(".ipynb" in stmt.lower() and _writes(stmt) for stmt in _statements(code)):
        return "non-atomic write to a .ipynb file"
    return None


def _assess(code: str, scope: str, where: str, advice: str) -> core.Result | None:
    """Blocco se `code` scrive un notebook in uno statement preciso; avviso se `code` scrive
    file e `scope` cita .ipynb altrove; altrimenti nulla."""
    hit = _notebook_write(code)
    if hit:
        return core.Result.deny(f"{hit} in {where}. {advice}. {_READS_OK}.")
    if ".ipynb" in scope.lower() and any(_writes(stmt) for stmt in _statements(code)):
        return core.Result.note(_WARN)
    return None


def _noise_spans(src: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """(inizio, fine) in (riga, colonna) di commenti e docstring, cioè stringhe che da sole
    formano uno statement: primo token significativo della riga logica, seguito da NEWLINE."""
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    spans = [(t.start, t.end) for t in toks if t.type == tokenize.COMMENT]
    sig = [t for t in toks if t.type not in _LAYOUT]
    for i, tok in enumerate(sig):
        first = i == 0 or sig[i - 1].type == tokenize.NEWLINE
        alone = i + 1 < len(sig) and sig[i + 1].type in (tokenize.NEWLINE, tokenize.ENDMARKER)
        if tok.type == tokenize.STRING and first and alone:
            spans.append((tok.start, tok.end))
    return spans


def _strip_comments_and_docstrings(src: str) -> str:
    """Toglie commenti e docstring Python lasciando le altre stringhe (dove vivono i percorsi .ipynb)."""
    try:
        spans = _noise_spans(src)
    except (tokenize.TokenError, SyntaxError):
        # I frammenti di Edit spesso non tokenizzano (parentesi o indentazione monche)
        return _LINE_COMMENT.sub(r"\1", src)
    # Stesse righe lette dal tokenizer, quindi stessa numerazione; i ritorni a capo restano
    starts = [0, *itertools.accumulate(len(line) for line in io.StringIO(src).readlines())]
    out, pos = [], 0
    for (r0, c0), (r1, c1) in sorted(spans):
        out.append(src[pos:starts[r0 - 1] + c0])
        pos = starts[r1 - 1] + c1
    return "".join(out) + src[pos:]


def _inline_code(cmd: str) -> str:
    """Il codice degli argomenti `-c`; il comando intero se non si scompone o non ne ha."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return cmd
    code = [arg for flag, arg in zip(argv, argv[1:]) if flag == "-c"]
    return "\n".join(code) if code else cmd


def _check_shell(cmd: str) -> core.Result | None:
    if ".ipynb" not in cmd.lower() and "nbformat" not in cmd:
        return None
    if _NBCONVERT.search(cmd):
        return core.Result.deny("`jupyter nbconvert --inplace` bypasses the atomic write and the lock. Use `nb execute <nb>`.")
    if not _PY_INLINE.search(cmd):
        return None
    # Il codice decide il blocco; la menzione di .ipynb vale anche fuori (es. in argv)
    return _assess(_inline_code(cmd), cmd, "a shell one-liner", _SKILL)


@core.on("PreToolUse", tools=core.WRITE_TOOLS + core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    if ctx.tool == "NotebookEdit":
        return core.Result.deny(f"NotebookEdit is disabled: every .ipynb write is atomic. {_SKILL}.")

    if ctx.tool in core.SHELL_TOOLS:
        return _check_shell(_util.command_of(ctx))

    path = _util.canon(_util.path_of(ctx), ctx.cwd if isinstance(ctx.cwd, str) else None)
    if path.endswith(".ipynb"):
        return core.Result.deny(
            f"direct {ctx.tool} on a .ipynb file is forbidden. {_SKILL}, or edit the paired .py and run `nb sync <nb>`."
        )
    if path.endswith(".py"):
        code = _strip_comments_and_docstrings(_util.new_text(ctx))
        return _assess(code, code, path, "Use nb_io.load_ipynb / nb_io.atomic_write_ipynb from the /notebook skill")
    return None
