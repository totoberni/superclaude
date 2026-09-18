"""Guardia content-scan: controlli sul testo appena scritto (ex guards/10-content-scan.sh).

Due classi:
1. DASH (correzione sul posto, mai un blocco): un trattino lungo (U+2014) o medio
   (U+2013) nel testo NUOVO viene sostituito prima che lo strumento scriva.
   Bloccare obbligherebbe il modello a rigenerare tutto l'output; sostituire costa
   una riga di nota. Si toccano solo le righe nuove: per Write quelle che il file
   su disco non contiene già, per Edit/MultiEdit quelle assenti da `old_string`
   (la regola guarda avanti: i trattini preesistenti restano come sono).
2. FIREWALL (bloccante): un riferimento interno a superclaude dentro un file sotto
   `~/code/`.

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
_FIREWALL_FIXED = (
    "~/.claude/", ".claude/rules", "agent-memory", "shared/projects/", "class/meta",
    "memory.md", "mtm.md", "ltm.md",
    ".memory.db", "memory_db.py",
    "meta says", "memory.md says", "according to the gotchas file", "see the project memory",
)
_CELL_ID = re.compile(r"\b(?:MM|GM|MT|CW|M|G|W)-[0-9]+")


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


def _firewall_hit(text: str) -> str | None:
    low = text.lower().replace("\\", "/")
    for pat in _FIREWALL_FIXED:
        if pat in low:
            return pat
    # Forma assoluta di ~/.claude/ (Windows `c:/...` e Git Bash `/c/...`)
    home = _util.canon(core.CLAUDE_HOME) + "/"
    msys = re.sub(r"^([a-z]):/", r"/\1/", home)
    if home in low or msys in low:
        return "~/.claude/"
    m = _CELL_ID.search(text)
    return f"memory cell id {m.group(0)}" if m else None


@core.on("PreToolUse", tools=core.WRITE_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
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
            hit = _firewall_hit(text)
            if hit:
                return core.Result.deny(
                    f"firewall: superclaude-internal reference '{hit}' in a project file under ~/code; "
                    "cite a local file or paraphrase inline instead"
                )
    return rewrite
