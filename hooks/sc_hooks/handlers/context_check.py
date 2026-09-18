"""context-check: avviso one-shot quando il contesto si avvicina all'auto-compact.

Legge `~/.claude/.context-latest.json`, scritto dalla statusline. Solo avviso,
non blocca mai. Vale per ogni ruolo. Se il file manca il costo è una sola
`open()` fallita.
"""
from __future__ import annotations

import json
import time

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

CONTEXT_FILE = core.CLAUDE_HOME / ".context-latest.json"

AUTOCOMPACT_MARGIN = 33000   # l'auto-compact scatta a window_size - 33000
WARN_MARGIN = 20000          # avviso entro 20000 token da quella soglia
STALE_AFTER_S = 3600         # campione più vecchio di un'ora: è di un'altra sessione


def _pos_int(value: object) -> int:
    """Intero positivo oppure 0 (bool, stringhe sporche e negativi valgono 0)."""
    if isinstance(value, bool):
        return 0
    try:
        n = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n > 0 else 0


@on("PreToolUse", name="context-check")
def handle(ctx: Ctx) -> Result | None:
    try:
        with open(CONTEXT_FILE, "rb") as fh:
            data = json.loads(fh.read(65536).decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    used, window = _pos_int(data.get("used_tokens")), _pos_int(data.get("window_size"))
    if not used or not window:
        return None
    ts = _pos_int(data.get("ts"))
    if ts and time.time() - ts > STALE_AFTER_S:
        return None
    autocompact = window - AUTOCOMPACT_MARGIN
    if used < autocompact - WARN_MARGIN:
        return None
    if not ctx.once("context-compact"):
        return None
    pct = data.get("used_pct")
    pct_txt = f"{pct:.0f}" if isinstance(pct, (int, float)) and not isinstance(pct, bool) else "?"
    return Result.note(
        f"Context ~{used}/{window} tok ({pct_txt}%); auto-compact near {autocompact}. "
        "Checkpoint state + stash recovery now."
    )
