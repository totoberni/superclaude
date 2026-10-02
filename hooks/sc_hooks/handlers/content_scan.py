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
2. FIREWALL (bloccante): un riferimento interno a superclaude dentro un file sotto
   `~/code/`. Gli id di cella a una lettera (M-, G-, W-) bloccano solo con un
   contesto di memoria sulla stessa riga; senza, producono un avviso.

I due caratteri vietati sono costruiti con `chr()` così questo sorgente non li contiene.
"""
from __future__ import annotations

import re
from pathlib import Path

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
    r"\b(?:memory|gotcha|mistake|lesson|good-idea|occ|occurrences?|superclaude)\b", re.I,
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
# Trattino lungo usato come inciso, con o senza spazi attorno: diventa "; "
_EM_RUN = re.compile(rf"[ \t]*{_EM}[ \t]*")
# Trattino medio tra cifre (intervalli: 10-20) e tra spazi (inciso); altrove un trattino semplice
_EN_RANGE = re.compile(rf"(?<=\d){_EN}(?=\d)")
_EN_SPACED = re.compile(rf"[ \t]+{_EN}[ \t]+")
_TRAILING_SEP = re.compile(r";[ \t]+$")


def _has_dash(text: str) -> bool:
    return any(d in text for d in _DASHES)


def _fix_line(line: str) -> str:
    line = _EN_RANGE.sub("-", line)
    line = _EN_SPACED.sub("; ", line)
    line = line.replace(_EN, "-")
    line = _EM_RUN.sub("; ", line)
    return _TRAILING_SEP.sub(";", line)


def _fix_new_lines(text: str, old_lines: set[str]) -> tuple[str, int]:
    """Corregge solo le righe nuove; restituisce (testo, numero di trattini sostituiti)."""
    fixed, count = [], 0
    for line in text.split("\n"):
        bare = line.rstrip("\r")
        if _has_dash(bare) and bare not in old_lines:
            count += sum(bare.count(d) for d in _DASHES)
            line = _fix_line(bare) + ("\r" if line.endswith("\r") else "")
        fixed.append(line)
    return "\n".join(fixed), count


def _disk_lines(path: str) -> set[str]:
    """Righe già presenti nel file su disco (vuoto se il file non c'è o è enorme)."""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > _MAX_OLD_FILE:
            return set()
        return set(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, ValueError):
        return set()


def _rewrite_dashes(ctx: core.Ctx, raw_path: str) -> tuple[dict | None, int]:
    """Copia di `tool_input` con i trattini nuovi sostituiti, oppure (None, 0)."""
    ti = dict(ctx.tool_input)
    total = 0

    def fix(value: object, old: set[str]) -> object:
        nonlocal total
        if not isinstance(value, str) or not _has_dash(value):
            return value
        new, n = _fix_new_lines(value, old)
        total += n
        return new

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
    return (ti, total) if total else (None, 0)


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

    rewrite: core.Result | None = None
    if _has_dash(text):
        fixed_input, count = _rewrite_dashes(ctx, raw_path)
        if fixed_input is not None:
            rewrite = core.Result.rewrite(
                fixed_input,
                f"content-scan: replaced {count} em/en dash(es) with ';' or '-' in the text you sent; "
                "the stored text differs from your draft on those lines.",
            )
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
                # Una sola risposta: l'eventuale riscrittura più entrambe le note
                if rewrite is None:
                    return core.Result.note(_suspect_note(suspects))
                rewrite.context.append(_suspect_note(suspects))
    return rewrite
