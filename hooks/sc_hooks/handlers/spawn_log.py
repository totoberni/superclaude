"""spawn-log: telemetria del ciclo di vita dei subagent (tre eventi, tre file TSV).

- PreToolUse su `Agent`   -> riga di lancio in `_spawns.log`
- PostToolUse su `Agent`  -> riga SPAWN in `_spawns-rich.log` (qui esiste l'agent id
                             del figlio) e riga di esito in `_outcomes.log`
- SubagentStop            -> righe EXIT e STOP in `_spawns.log`, riga EXIT in
                             `_spawns-rich.log`

I layout delle colonne sono un contratto: skill e statusline li leggono.
  _spawns.log       ts, parent, subagent_type, description(<=80)
                    ts, "", agent_type, EXIT, agent_id
                    ts, "", agent_type, STOP, agent_id, duration_s, status, transcript_path
  _spawns-rich.log  ts, agent_id, subagent_type, SPAWN|EXIT, short_desc(<=40), outcome
  _outcomes.log     ts, parent, subagent_type, description(<=120), outcome, last_120_chars
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

COMMS_DIR = core.CLAUDE_HOME / "comms"

_OUTCOMES_HEADER = (
    "# Agent outcome log. TSV: timestamp\\tparent_agent\\tsubagent_type\\tdescription\\toutcome\\tlast_120_chars\n"
    "# Outcomes: OK | TRUNCATED | FAILED | UNKNOWN\n"
    "# Source hook: ~/.claude/hooks/sc_hooks/handlers/spawn_log.py\n"
)
_FAIL_RE = r"(^|\b)(Error:|Failed:|FAILED:|BLOCKED:|Traceback \(most recent|fatal:|panic:)"
_STOP_FAIL_RE = (r"(^|\b)(Error:|Failed:|FAILED:|BLOCKED:|Blocker:|Traceback \(most recent|fatal:|panic:"
                 r"|### Blockers)")
_OK_RE = r"^## (Verification|Files changed|Diff|Output|Summary|Result|Done|Notes)"
_TRUNC_RE = (r"(let me (try|retry|check|see|look|read|run|think|investigate|verify|examine|inspect|continue"
             r"|figure|do)|what did that touch|wait,? let me|hmm,? |actually,? |OK let me)")
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return time.strftime(_TS_FMT, time.gmtime())


def _cell(value: Any, limit: int | None = None) -> str:
    """Valore per una cella TSV: tronca, poi toglie tab e a capo (niente righe forgiate)."""
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    if limit is not None:
        s = s[:limit]
    return s.replace("\t", "").replace("\n", "").replace("\r", "")


def _safe_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", value) if isinstance(value, str) else ""


def _append(name: str, row: list[str], header: str = "") -> None:
    """Accoda una riga; ogni errore di filesystem viene ignorato."""
    try:
        COMMS_DIR.mkdir(parents=True, exist_ok=True)
        path = COMMS_DIR / name
        if header and (not path.exists() or path.stat().st_size == 0):
            row_text = header + "\t".join(row) + "\n"
        else:
            row_text = "\t".join(row) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(row_text)
    except OSError:
        pass


def _parent(ctx: Ctx) -> str:
    return _cell(ctx.agent) or "unknown"


def _subagent_type(ctx: Ctx) -> str:
    return _cell(ctx.tool_input.get("subagent_type")) or "unknown"


# --------------------------------------------------------------------------
# PreToolUse: riga di lancio
# --------------------------------------------------------------------------
@on("PreToolUse", tools=("Agent",), name="spawn-log")
def on_spawn(ctx: Ctx) -> Result | None:
    _append("_spawns.log", [_now(), _parent(ctx), _subagent_type(ctx),
                            _cell(ctx.tool_input.get("description"), 80)])
    return None


# --------------------------------------------------------------------------
# PostToolUse: riga SPAWN ricca + classificazione dell'esito
# --------------------------------------------------------------------------
def _blocks_text(items: list[Any]) -> str:
    parts: list[str] = []
    for it in items:
        if isinstance(it, str):
            parts.append(it)
        elif isinstance(it, dict) and it.get("text") not in (None, False):
            t = it["text"]
            parts.append(t if isinstance(t, str) else json.dumps(t, ensure_ascii=False))
        else:
            parts.append(json.dumps(it, ensure_ascii=False, default=str))
    return "\n".join(parts)


def response_text(resp: Any) -> str:
    """Testo visibile del worker: `tool_response` può essere stringa, lista di blocchi o dict."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return _blocks_text(resp)
    if isinstance(resp, dict):
        inner: Any = resp
        for key in ("content", "text", "output"):
            if resp.get(key) not in (None, False):
                inner = resp[key]
                break
        if isinstance(inner, str):
            return inner
        if isinstance(inner, list):
            return _blocks_text(inner)
        if isinstance(inner, dict):
            t = inner.get("text")
            return t if isinstance(t, str) and t else json.dumps(inner, ensure_ascii=False, default=str)
        return str(inner)
    return str(resp)


def classify(text: str) -> str:
    """OK | TRUNCATED | FAILED | UNKNOWN. Ordine: FAILED, poi OK, poi TRUNCATED."""
    full_len = len(text)
    if full_len > 51200:
        text = text[-5120:]                       # output enorme: si guarda solo la coda
    if re.search(_FAIL_RE, text[-2048:], re.MULTILINE):
        return "FAILED"
    if re.search(_OK_RE, text, re.MULTILINE):
        return "OK"
    if full_len > 80:
        tail = text[-240:].rstrip()
        if re.search(_TRUNC_RE, tail, re.IGNORECASE):
            return "TRUNCATED"
        if tail and tail[-1] not in ".!?)]}\"`>":
            return "TRUNCATED"                    # finisce a metà frase, senza marcatori di chiusura
    return "OK" if full_len > 0 else "UNKNOWN"


@on("PostToolUse", tools=("Agent",), name="spawn-log")
def on_outcome(ctx: Ctx) -> Result | None:
    resp = ctx.payload.get("tool_response")
    subagent = _subagent_type(ctx)
    description = _cell(ctx.tool_input.get("description"), 120)
    child_id = _safe_id(resp.get("agentId")) if isinstance(resp, dict) else ""
    now = _now()

    _append("_spawns-rich.log", [now, child_id, subagent, "SPAWN", description[:40], ""])

    text = response_text(resp)
    last_120 = _cell(text[-120:]) or "(empty)"
    _append("_outcomes.log", [now, _parent(ctx), subagent, description, classify(text), last_120],
            header=_OUTCOMES_HEADER)
    return None


# --------------------------------------------------------------------------
# SubagentStop: righe EXIT e STOP
# --------------------------------------------------------------------------
def _spawn_ts(rich_log: Path, agent_id: str) -> str:
    """Timestamp dell'ultima riga SPAWN con questo agent id (si legge solo la coda del file)."""
    try:
        with rich_log.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 262144))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = ""
    for line in tail.splitlines():
        cols = line.split("\t")
        if len(cols) >= 4 and cols[1] == agent_id and cols[3] == "SPAWN":
            found = cols[0]
    return found


def _duration_s(spawn_ts: str, stop_ts: str) -> str:
    from datetime import datetime
    try:
        delta = datetime.strptime(stop_ts, _TS_FMT) - datetime.strptime(spawn_ts, _TS_FMT)
    except (ValueError, TypeError):
        return ""
    secs = int(delta.total_seconds())
    return str(secs) if secs >= 0 else ""


@on("SubagentStop", name="spawn-log")
def on_stop(ctx: Ctx) -> Result | None:
    p = ctx.payload
    agent_id = _safe_id(p.get("agent_id") or p.get("subagent_id"))
    agent_type = _cell(p.get("agent_type") or p.get("subagent_type")) or "unknown"
    ts = _now()

    # Riga EXIT storica: la statusline conta gli sciami attivi su col4 == "EXIT"
    _append("_spawns.log", [ts, "", agent_type, "EXIT", agent_id])

    last_msg = p.get("last_assistant_message")
    outcome = ""
    if isinstance(last_msg, str) and last_msg:
        outcome = "fail" if re.search(_STOP_FAIL_RE, last_msg[-2048:], re.MULTILINE) else "ok"

    # La durata si ricava accoppiando l'agent id alla riga SPAWN, prima di scrivere la EXIT
    duration = ""
    if agent_id:
        spawn_ts = _spawn_ts(COMMS_DIR / "_spawns-rich.log", agent_id)
        if spawn_ts:
            duration = _duration_s(spawn_ts, ts)

    _append("_spawns-rich.log", [ts, agent_id, agent_type, "EXIT", "", outcome])

    transcript = _cell(p.get("agent_transcript_path") or p.get("transcript_path"))
    _append("_spawns.log", [ts, "", agent_type, "STOP", agent_id or "unknown", duration, outcome, transcript])
    return None
