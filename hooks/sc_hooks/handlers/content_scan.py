"""Guardia content-scan: controlli sul testo appena scritto (ex guards/10-content-scan.sh).

Due classi, entrambe bloccanti:
1. DASH: un trattino lungo (U+2014) o medio (U+2013) nel testo NUOVO. Per Edit e
   MultiEdit il testo è già un diff; per Write su un file esistente contano solo
   le righe che il file su disco non contiene già (la regola guarda avanti: i
   trattini preesistenti non bloccano una riscrittura).
2. FIREWALL: un riferimento interno a superclaude dentro un file sotto `~/code/`.

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


def _has_dash(text: str) -> bool:
    return any(d in text for d in _DASHES)


def _added_lines(path: str, text: str) -> str:
    """Righe di `text` che non esistono già, identiche, nel file su disco."""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > _MAX_OLD_FILE:
            return text
        old = set(p.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, ValueError):
        return text
    return "\n".join(line for line in text.splitlines() if line not in old)


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


@core.on("PreToolUse", tools=core.WRITE_TOOLS + ("Agent",), name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    text = _util.new_text(ctx)
    if ctx.tool == "Agent":
        prompt = _util.field(ctx, "prompt")
        text = prompt if isinstance(prompt, str) else ""
    if not text:
        return None

    raw_path = _util.path_of(ctx) if ctx.tool != "Agent" else ""

    if _has_dash(text):
        scan = text
        if ctx.tool == "Write" and raw_path:
            # norm_path restituisce `c:/...`, che Windows apre senza problemi
            scan = _added_lines(_util.canon(raw_path, ctx.cwd), text)
        if _has_dash(scan):
            return core.Result.deny("em dash or en dash in new text; use ; : , . or parentheses instead")

    if raw_path:
        path = _util.canon(raw_path, ctx.cwd)
        if _util.under(path, core.WORKSPACE_ROOT) and not _util.under(path, core.CLAUDE_HOME):
            hit = _firewall_hit(text)
            if hit:
                return core.Result.deny(
                    f"firewall: superclaude-internal reference '{hit}' in a project file under ~/code; "
                    "cite a local file or paraphrase inline instead"
                )
    return None
