"""thinking-nudge: consiglio one-shot sulla profondità di ragionamento dei worker.

PreToolUse su `Agent`. Se la matrice dei worker prescrive un ragionamento più
profondo per il tipo di subagent lanciato e il prompt non contiene già una parola
chiave di thinking, emette una nota (mai un blocco). Una sola volta per tipo di
worker per sessione, per non fare rumore nei lanci paralleli.
"""
from __future__ import annotations

from sc_hooks.core import Ctx, Result, on

_KEYWORDS = r"\b(ultrathink|megathink|think harder|think hard|think)\b"

# Per ogni worker: lista ordinata di (regex sul prompt minuscolo | None, classe del compito).
# `None` è il caso di default. Worker assenti dalla tabella: nessuna nota.
_MATRIX: dict[str, tuple[tuple[str | None, str], ...]] = {
    "w-debugger": (
        (r"\b(race condition|race-condition|multi-file|across (files|modules)|cross-file)\b",
         "multi-file race / cross-file"),
        (None, "single-file debug"),
    ),
    "w-planner": (
        (r"\b(architect|architectural|architecture|3\+ phase|multi-phase|irreversible)\b",
         "architectural / multi-phase planning"),
        (None, "single-phase planning"),
    ),
    "w-reviewer": (
        (r"(scathingly[- ]deep|--scathingly-deep|architectural review|deep review)",
         "scathingly-deep / architectural review"),
    ),
    "w-doc": (
        (r"\b(cross[- ]section|coherence|architectural|cross-cutting|consistency across)\b",
         "cross-section / coherence doc work"),
    ),
    "w-implementer": (
        (r"\b([5-9]|[1-9][0-9])\s*(files|modules|components)\b"
         r"|\b(architect|architecture|cross-cutting|web app|whole-app)\b",
         "multi-file / architectural implementation"),
    ),
    "w-merger": (
        (r"\b(semantic|race|three-way|complex conflict)\b", "semantic merge conflict"),
    ),
}


def task_class(subagent: str, prompt: str) -> str:
    """Classe del compito che merita la nota, oppure stringa vuota."""
    rules = _MATRIX.get(subagent)
    if not rules or not prompt:
        return ""
    import re
    if re.search(_KEYWORDS, prompt, re.IGNORECASE):
        return ""                                  # parola chiave già presente
    low = prompt.lower()
    for pattern, label in rules:
        if pattern is None or re.search(pattern, low):
            return label
    return ""


@on("PreToolUse", tools=("Agent",), name="thinking-nudge")
def handle(ctx: Ctx) -> Result | None:
    subagent, prompt = ctx.tool_input.get("subagent_type"), ctx.tool_input.get("prompt")
    if not isinstance(subagent, str) or not isinstance(prompt, str):
        return None
    subagent = subagent.strip()
    label = task_class(subagent, prompt)
    if not label or not ctx.once(f"thinking-nudge-{subagent}"):
        return None
    return Result.note(
        f"{subagent} on {label} tasks benefits from elevated reasoning depth, but prompt thinking "
        "keywords are no-ops on adaptive-thinking models. Set depth via the target agent's "
        "frontmatter `effort:` (low/medium/high/xhigh/max), or override model/effort at dispatch; "
        "do NOT embed a prompt keyword. See rules/13-worker-first-mandate.md section Critical "
        "Implementation Note. Advisory only; proceeding with dispatch as-is."
    )
