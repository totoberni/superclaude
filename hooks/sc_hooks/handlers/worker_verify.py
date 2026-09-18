"""Guardia worker-verify: promemoria R-3 al ritorno di un subagent (ex guards/80-worker-verify.sh).

Un hook PostToolUse non può obbligare a leggere il diff: rende solo visibile
l'obbligo. La versione bash scriveva su stderr, dove il modello non la vedeva
mai; qui la nota passa da additionalContext. Bash non aveva alcun limite di
frequenza, quindi il testo completo esce una sola volta per sessione (`ctx.once`).
Niente `git diff --stat` dal vivo: l'hook non sa quale repository ha toccato il worker.
"""
from __future__ import annotations

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "worker-verify"


@core.on("PostToolUse", tools=("Agent",), name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    kind = _util.field(ctx, "subagent_type")
    who = kind.strip() if isinstance(kind, str) and kind.strip() else "subagent"
    if not ctx.once("worker-verify"):
        return None
    return core.Result.note(
        f"R-3 verification due for '{who}' (and for every later agent return this session): before trusting "
        "the report run 'git diff --stat', read the files it touched, run the tests if applicable, and check "
        "for weakened assertions, added skips and scope violations."
    )
