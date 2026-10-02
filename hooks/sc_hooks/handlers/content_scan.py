"""Guardia content-scan: controlli sul testo appena scritto (ex guards/10-content-scan.sh).

Due classi:
1. DASH (correzione sul posto, mai un blocco): un trattino lungo (U+2014) o medio
   (U+2013) nel testo NUOVO viene sostituito prima che lo strumento scriva.
   Bloccare obbligherebbe il modello a rigenerare tutto l'output; sostituire costa
   una riga di nota. Si toccano solo le righe nuove: per Write quelle che il file
   su disco non contiene già, per Edit/MultiEdit quelle assenti da `old_string`
   (la regola guarda avanti: i trattini preesistenti restano come sono).
   Vale anche per i messaggi di commit scritti nella shell (Bash/PowerShell con
   `git commit`): lì tutto il testo è nuovo, quindi si corregge ogni trattino.
   Nel codice un trattino è spesso un dato (regex, fixture, stringa di confronto):
   nei file di codice si corregge solo la parte di commento, nei file di dati
   nulla, nella prosa nulla dentro i blocchi recintati markdown. Ogni trattino
   lasciato produce un avviso (mai un blocco).
2. FIREWALL (bloccante): un riferimento interno a superclaude dentro un file sotto
   `~/code/`. Gli id di cella a una lettera (M-, G-, W-) bloccano solo con un
   contesto di memoria sulla stessa riga; senza, producono un avviso.

I due caratteri vietati sono costruiti con `chr()` così questo sorgente non li contiene.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "content-scan"

_DASHES = (chr(0x2014), chr(0x2013))
_MAX_OLD_FILE = 5_000_000          # oltre, niente diff: si analizza tutto il testo

# Elenco dei pattern vietati: rules/20-tool-conventions.md, sezione Firewall.
# Confronto senza distinzione tra maiuscole e con gli slash normalizzati.
_FIREWALL_PATHS = (
    "~/.claude/", ".claude/rules", "agent-memory", "shared/projects/", "class/meta",
    "memory.md", "mtm.md", "ltm.md",
    ".memory.db", "memory_db.py",
)
_FIREWALL_PHRASES = (
    "meta says", "memory.md says", "according to the gotchas file", "see the project memory",
)


def _token_pattern(pat: str) -> re.Pattern[str]:
    """I percorsi valgono solo come token interi: `src/subclass/metadata.py` non è `class/meta`."""
    tail = r"(?![a-z0-9_])" if re.match(r"\w", pat[-1]) else ""
    return re.compile(r"(?<![a-z0-9_])" + re.escape(pat) + tail)


_FIREWALL_PATH_RES = tuple((pat, _token_pattern(pat)) for pat in _FIREWALL_PATHS)
_CELL_ID = re.compile(r"\b(?P<prefix>MM|GM|MT|CW|M|G|W)-[0-9]+")
# Con un prefisso di una lettera serve un indizio di memoria sulla stessa riga:
# da soli "Form W-2" o "M-16" sono testo normale di progetto.
_CELL_CONTEXT = re.compile(
    r"\b(?:memory|memories|gotchas?|mistakes?|lessons?|good-ideas?|occ|occurrences?|superclaude)\b", re.I,
)
_TABLE_OPEN = re.compile(r"\|[ \t]*$")
_TABLE_CLOSE = re.compile(r"[ \t]*\|")
_MAX_SUSPECTS = 5

# Strumenti per cui un trattino nel comando può essere voluto (pattern, sostituzioni):
# lì la riscrittura cambierebbe il significato. Il punto prima esclude le estensioni
# (`a.py`), lo slash dopo esclude le cartelle (`/tr/`).
_DASH_SENSITIVE = re.compile(
    r"(?<![\w.-])(?:grep|rg|sed|awk|perl|tr|python[0-9.]*|py|node|findstr|select-string|-replace|-match|-split)(?![\w/\\-])",
    re.I,
)


_EM, _EN = _DASHES
# Trattino lungo usato come inciso, con o senza spazi attorno: diventa "; ".
# Una serie di trattini lunghi consecutivi vale come uno solo.
_EM_RUN = re.compile(rf"[ \t]*{_EM}(?:[ \t]*{_EM})*[ \t]*")
# Trattino medio tra cifre (intervalli: 10-20) e tra spazi (inciso); altrove un trattino semplice
_EN_RANGE = re.compile(rf"(?<=\d){_EN}(?=\d)")
_EN_SPACED = re.compile(rf"[ \t]+{_EN}[ \t]+")
_TRAILING_SEP = re.compile(r";[ \t]+$")
# Un trattino che occupa da solo una cella di tabella markdown vuol dire "vuoto": resta un trattino
_TABLE_CELL_DASH = re.compile(rf"(?<=\|)([ \t]*)[{_EM}{_EN}]([ \t]*)(?=\|)")

# Marcatore del commento di riga per estensione: nei file di codice si corregge solo il commento
_COMMENT_MARKER = {
    **dict.fromkeys(("py", "pyi", "sh", "bash", "ps1", "psm1", "r", "rb", "jl", "yaml", "yml", "toml", "cfg"), "#"),
    **dict.fromkeys(("js", "jsx", "ts", "tsx", "mjs", "cjs", "gs", "java", "c", "h", "cpp", "hpp", "cc", "cs",
                     "go", "rs", "php", "scala", "kt", "swift", "v", "sv"), "//"),
    **dict.fromkeys(("sql", "lua", "vhd"), "--"),
}
# Nei file di dati ogni trattino è contenuto: non si corregge mai
_DATA_EXTS = frozenset(("json", "jsonl", "csv", "tsv", "xml", "ipynb"))


def _comment_re(marker: str) -> re.Pattern[str]:
    """Come `_LINE_COMMENT` di notebook_guard: il marcatore conta solo fuori da virgolette chiuse sulla riga."""
    m = re.escape(marker)
    return re.compile(rf"""((?:(?!{m})[^'"]|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")*){m}""")


_COMMENT_RES = {mk: _comment_re(mk) for mk in set(_COMMENT_MARKER.values())}
# Righe di un commento a blocco in stile C (`/* ...`, ` * ...`, ` */`). Lo `*` deve essere
# seguito da spazio, `/` o fine riga, così `*ptr = ...` resta codice.
_BLOCK_COMMENT_LINE = re.compile(r"[ \t]*(?:/\*|\*(?=[ \t/]|$))")
# Apertura o chiusura di un blocco recintato markdown
_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
_MAX_LEFT_LINES = 5


@dataclass
class _DashTally:
    """Trattini sostituiti e trattini lasciati nel codice o nei dati (con le righe, base 1)."""
    replaced: int = 0
    left: int = 0
    left_lines: list[int] = field(default_factory=list)

    def add(self, before: str, after: str, line_no: int) -> None:
        left = _dash_count(after)
        self.replaced += _dash_count(before) - left
        if left:
            self.left += left
            self.left_lines.append(line_no)


def _has_dash(text: str) -> bool:
    return any(d in text for d in _DASHES)


def _dash_count(text: str) -> int:
    return sum(text.count(d) for d in _DASHES)


def _fix_line(line: str) -> str:
    line = _TABLE_CELL_DASH.sub(r"\1-\2", line)
    line = _EN_RANGE.sub("-", line)
    line = _EN_SPACED.sub("; ", line)
    line = line.replace(_EN, "-")
    line = _EM_RUN.sub("; ", line)
    return _TRAILING_SEP.sub(";", line)


def _code_marker(path: str) -> str | None:
    """Marcatore di commento per il codice, "" per i dati (nessun commento), None per la prosa."""
    ext = PurePosixPath(path.replace("\\", "/")).suffix.lower().lstrip(".")
    return "" if ext in _DATA_EXTS else _COMMENT_MARKER.get(ext)


def _fix_code_line(line: str, marker: str) -> str:
    """Corregge solo il commento della riga; la parte di codice resta identica byte per byte."""
    if not marker:
        return line
    if marker == "//" and _BLOCK_COMMENT_LINE.match(line):
        return _fix_line(line)
    m = _COMMENT_RES[marker].match(line)
    if m is None:
        return line
    cut = m.end(1)
    return line[:cut] + _fix_line(line[cut:])


def _fence_step(fence: str | None, line: str) -> tuple[str | None, bool]:
    """(recinzione aperta dopo `line`, `line` è codice). Le righe di recinzione contano come codice."""
    m = _FENCE.match(line)
    if fence is None:
        return (m.group(1), True) if m else (None, False)
    closes = bool(m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[m.end():].strip())
    return (None if closes else fence), True


def _fix_text(text: str, old_lines: set[str], marker: str | None, tally: _DashTally) -> str:
    """Corregge le righe nuove secondo il tipo di file (marker: vedi `_code_marker`).

    Nella prosa le righe dentro un blocco recintato si trattano come codice senza commenti.
    Un frammento di Edit parte sempre fuori da un blocco recintato.
    """
    out: list[str] = []
    fence: str | None = None
    for n, line in enumerate(text.split("\n"), 1):
        bare = line.rstrip("\r")
        line_marker = marker
        if marker is None:
            fence, in_fence = _fence_step(fence, bare)
            line_marker = "" if in_fence else None
        if _has_dash(bare) and bare not in old_lines:
            new = _fix_line(bare) if line_marker is None else _fix_code_line(bare, line_marker)
            tally.add(bare, new, n)
            line = new + line[len(bare):]
        out.append(line)
    return "\n".join(out)


def _fix_new_lines(text: str, old_lines: set[str]) -> tuple[str, int]:
    """Corregge solo le righe nuove come prosa; restituisce (testo, numero di trattini sostituiti)."""
    tally = _DashTally()
    return _fix_text(text, old_lines, None, tally), tally.replaced


def _disk_lines(path: str) -> set[str]:
    """Righe già presenti nel file su disco (vuoto se il file non c'è o è enorme)."""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > _MAX_OLD_FILE:
            return set()
        return set(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, ValueError):
        return set()


def _rewrite_dashes(ctx: core.Ctx, raw_path: str) -> tuple[dict | None, _DashTally]:
    """(copia di `tool_input` con i trattini nuovi sostituiti oppure None, conteggio)."""
    ti = dict(ctx.tool_input)
    marker = _code_marker(raw_path)
    tally = _DashTally()

    def fix(value: object, old: set[str]) -> object:
        if not isinstance(value, str) or not _has_dash(value):
            return value
        return _fix_text(value, old, marker, tally)

    if ctx.tool == "Write":
        old = _disk_lines(_util.canon(raw_path, ctx.cwd)) if raw_path else set()
        ti["content"] = fix(ti.get("content"), old)
    else:
        old_string = ti.get("old_string")
        old = set(old_string.splitlines()) if isinstance(old_string, str) else set()
        for key in ("new_string", "new_source"):
            if key in ti:
                ti[key] = fix(ti[key], old)
        edits = ti.get("edits")
        if isinstance(edits, list):
            new_edits = []
            for edit in edits:
                if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                    eo = edit.get("old_string")
                    edit = {**edit, "new_string": fix(edit["new_string"], set(eo.splitlines()) if isinstance(eo, str) else set())}
                new_edits.append(edit)
            ti["edits"] = new_edits
    return (ti if tally.replaced else None), tally


def _left_note(tally: _DashTally) -> str:
    lines = tally.left_lines
    shown = ", ".join(map(str, lines[:_MAX_LEFT_LINES])) + (", ..." if len(lines) > _MAX_LEFT_LINES else "")
    return (f"content-scan: left {tally.left} em/en dash(es) in code or data untouched (lines {shown}); "
            "if any is prose (docstring, UI string), reword it; if it is data (regex, fixture), keep it")


def _dash_fixes(ctx: core.Ctx, raw_path: str) -> tuple[dict | None, list[str]]:
    """(input corretto oppure None, note per il modello) per i trattini del testo nuovo."""
    fixed_input, tally = _rewrite_dashes(ctx, raw_path)
    notes: list[str] = []
    if fixed_input is not None:
        notes.append(f"content-scan: replaced {tally.replaced} em/en dash(es) with ';' or '-' in the text you sent; "
                     "the stored text differs from your draft on those lines.")
    if tally.left:
        notes.append(_left_note(tally))
    return fixed_input, notes


def _is_cell_ref(line: str, m: re.Match[str]) -> bool:
    """Vero se l'id trovato è con ogni probabilità una cella di memoria."""
    if len(m.group("prefix")) > 1 or _CELL_CONTEXT.search(line):
        return True
    # Forma tabella delle celle: `| M-4 |`
    return bool(_TABLE_OPEN.search(line[:m.start()]) and _TABLE_CLOSE.match(line[m.end():]))


def _firewall_scan(text: str) -> tuple[str | None, list[str]]:
    """(riferimento da bloccare oppure None, id di cella solo sospetti da segnalare)."""
    low = text.lower().replace("\\", "/")
    for pat, rx in _FIREWALL_PATH_RES:
        if rx.search(low):
            return pat, []
    for pat in _FIREWALL_PHRASES:
        if pat in low:
            return pat, []
    # Forma assoluta di ~/.claude/ (Windows `c:/...` e Git Bash `/c/...`)
    home = _util.canon(core.CLAUDE_HOME) + "/"
    msys = re.sub(r"^([a-z]):/", r"/\1/", home)
    if home in low or msys in low:
        return "~/.claude/", []
    suspects: list[str] = []
    for line in text.splitlines():
        for m in _CELL_ID.finditer(line):
            if _is_cell_ref(line, m):
                return f"memory cell id {m.group(0)}", []
            if m.group(0) not in suspects:
                suspects.append(m.group(0))
    return None, suspects


def _suspect_note(ids: list[str]) -> str:
    shown = ", ".join(f"'{i}'" for i in ids[:_MAX_SUSPECTS]) + (", ..." if len(ids) > _MAX_SUSPECTS else "")
    what = "looks like a memory cell id" if len(ids) == 1 else "look like memory cell ids"
    return (f"content-scan: {shown} {what}; if it is one, remove it "
            "(project files must not cite superclaude memory)")


def _commit_dashes(ctx: core.Ctx) -> core.Result | None:
    """Trattini nel testo di un `git commit` lanciato da Bash/PowerShell."""
    raw = _util.field(ctx, "command")
    if not isinstance(raw, str) or not _has_dash(raw):
        return None
    cmd = _util.command_of(ctx)
    commits = [c for c in _util.git_calls(cmd, ctx.cwd) if c.verb == "commit"]
    if not commits:
        return None
    # Gli strumenti sensibili si cercano fuori dai segmenti di commit, così un
    # messaggio che nomina "python" o "grep" resta correggibile.
    rest = cmd
    for c in commits:
        rest = rest.replace(c.segment, " ")
    m = _DASH_SENSITIVE.search(rest)
    if m:
        return core.Result.note(
            "content-scan: commit message contains em/en dash; not auto-fixed because the "
            f"command also runs {m.group(0)}; reword it"
        )
    fixed, count = _fix_new_lines(raw, set())
    return core.Result.rewrite(
        {**ctx.tool_input, "command": fixed},
        f"content-scan: replaced {count} em/en dash(es) with ';' or '-' in the commit message; "
        "the commit text differs from your draft on those lines.",
    )


@core.on("PreToolUse", tools=core.WRITE_TOOLS + core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    if ctx.tool in core.SHELL_TOOLS:
        return _commit_dashes(ctx)
    # I prompt di dispatch (Agent) non sono documenti: la regola sui trattini
    # riguarda il testo che resta su disco, quindi qui non vengono esaminati.
    if ctx.tool not in core.WRITE_TOOLS:
        return None
    text = _util.new_text(ctx)
    if not text:
        return None

    raw_path = _util.path_of(ctx)

    fixed_input, notes = _dash_fixes(ctx, raw_path) if _has_dash(text) else (None, [])
    if fixed_input is not None:
        # Il firewall qui sotto deve vedere il testo corretto
        ctx.tool_input = fixed_input
        text = _util.new_text(ctx)

    if raw_path:
        path = _util.canon(raw_path, ctx.cwd)
        if _util.under(path, core.WORKSPACE_ROOT) and not _util.under(path, core.CLAUDE_HOME):
            hit, suspects = _firewall_scan(text)
            if hit:
                return core.Result.deny(
                    f"firewall: superclaude-internal reference '{hit}' in a project file under ~/code; "
                    "cite a local file or paraphrase inline instead"
                )
            if suspects:
                notes.append(_suspect_note(suspects))
    # Una sola risposta: l'eventuale riscrittura più tutte le note
    if fixed_input is None and not notes:
        return None
    return core.Result(updated_input=fixed_input, context=notes)
