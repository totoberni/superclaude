"""snapshots: tracce di recupero prima del compact (PreCompact) e a fine turno (Stop).

PreCompact scrive `compact-<timestamp>-<sid8>.md` con: intestazione, testa dei file
di stato dei piani (`~/.claude/plans/*/state*.md`), attività recente ricavata dalla
coda del transcript e coda dello spawn log. Stop copia la coda dello spawn log (e
gli agenti `_ephemeral`, se esistono) in una cartella per sessione. In entrambi i
casi restano solo i 5 snapshot più recenti. Nessun output verso il modello.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

SYSTEM_DIR = core.CLAUDE_HOME / "agent-memory" / "_system"
COMPACT_DIR = SYSTEM_DIR / "_compact-snapshots"
STOP_DIR = SYSTEM_DIR / "_stop-snapshots"
PLANS_DIR = core.CLAUDE_HOME / "plans"
SPAWN_LOG = core.CLAUDE_HOME / "comms" / "_spawns.log"
EPHEMERAL_DIR = core.CLAUDE_HOME / "agents" / "_ephemeral"

KEEP = 5
TRANSCRIPT_LINES = 400
TRANSCRIPT_MAX_BYTES = 4 * 1024 * 1024
STOP_MAX_AGE_S = 7 * 86400


def _fs(raw: Any) -> Path | None:
    """Percorso utilizzabile dal filesystem Windows (accetta anche `/c/Users/...`)."""
    s = raw.strip() if isinstance(raw, str) else ""
    if not s:
        return None
    m = re.match(r"^/([a-zA-Z])(/|$)", s)
    if m:
        s = f"{m.group(1)}:/{s[m.end():]}"
    return Path(s).expanduser()


def _tail_lines(path: Path, n: int, max_bytes: int = TRANSCRIPT_MAX_BYTES) -> list[str]:
    """Ultime `n` righe leggendo al massimo `max_bytes` dalla fine del file."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]                          # la prima riga è quasi certamente monca
    return lines[-n:]


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _rotate(paths: list[Path], keep: int = KEEP) -> None:
    """Tiene i `keep` più recenti (per mtime, poi per nome) e cancella il resto."""
    for old in sorted(paths, key=lambda p: (_mtime(p), p.name), reverse=True)[keep:]:
        _remove(old)


def _remove(p: Path) -> None:
    try:
        if p.is_dir() and not p.is_symlink():
            import shutil
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    except OSError:
        pass


def transcript_activity(path: Path) -> tuple[str, list[str]]:
    """(ultimo messaggio utente, fino a 8 file toccati di recente, il più recente per primo)."""
    last_user = ""
    files: dict[str, None] = {}
    for line in _tail_lines(path, TRANSCRIPT_LINES):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if entry.get("type") == "user":
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(str(b.get("text") or "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = ""
            text = " ".join(text.split())
            if text:
                last_user = text
        elif entry.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") not in ("Edit", "Write", "Read"):
                    continue
                inp = block.get("input")
                fp = (inp.get("file_path") or inp.get("notebook_path")) if isinstance(inp, dict) else None
                if isinstance(fp, str) and fp:
                    files.pop(fp, None)            # reinserito in coda: conta l'ultimo tocco
                    files[fp] = None
    return last_user[:400], list(files)[-8:][::-1]


def _compact_dir() -> Path:
    override = os.environ.get("COMPACT_SNAPSHOT_DIR", "").strip()
    return (_fs(override) or COMPACT_DIR) if override else COMPACT_DIR


def build_compact_snapshot(ctx: Ctx) -> str:
    out = [
        "# Compact Snapshot",
        f"**Time**: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"**Session**: {ctx.session_id or 'unknown'}",
        f"**Agent**: {ctx.agent or 'unknown'}",
        f"**Trigger**: {ctx.payload.get('trigger') or 'unknown'}",
        "",
        "## Active State Files",
    ]
    try:
        state_files = sorted(PLANS_DIR.glob("*/state*.md"))
    except OSError:
        state_files = []
    for f in state_files:
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                head = [next(fh, "") for _ in range(20)]
        except OSError:
            continue
        out += ["", f"### {f.parent.name}/{f.name}", "```", "".join(head).rstrip("\n"), "```"]

    transcript = _fs(ctx.payload.get("transcript_path"))
    if transcript is not None:
        last_user, files = transcript_activity(transcript)
        if last_user or files:
            out += ["", "## Recent Activity (from transcript)"]
            if last_user:
                out.append(f"**Last user message**: {last_user}")
            if files:
                out.append("**Files recently touched** (most-recent first):")
                out += [f"  - {fp}" for fp in files]

    spawns = _tail_lines(SPAWN_LOG, 20, 65536)
    if spawns:
        out += ["", "## Recent Spawns (tail of _spawns.log)", "```", *spawns, "```"]
    return "\n".join(out) + "\n"


@on("PreCompact", name="snapshots")
def on_pre_compact(ctx: Ctx) -> Result | None:
    try:
        snap_dir = _compact_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        sid8 = re.sub(r"[^A-Za-z0-9_-]", "_", ctx.session_id or "unknown")[:8]
        target = snap_dir / f"compact-{time.strftime('%Y%m%d-%H%M%S')}-{sid8}.md"
        target.write_text(build_compact_snapshot(ctx), encoding="utf-8", newline="\n")
        _rotate(list(snap_dir.glob("compact-*.md")))
    except OSError:
        pass
    return None


@on("Stop", name="snapshots")
def on_stop(ctx: Ctx) -> Result | None:
    if not ctx.session_id:
        return None
    spawns = _tail_lines(SPAWN_LOG, 100, 262144)
    try:
        has_ephemeral = EPHEMERAL_DIR.is_dir() and any(EPHEMERAL_DIR.iterdir())
    except OSError:
        has_ephemeral = False
    if not spawns and not has_ephemeral:
        return None                                # niente da salvare: nessuna cartella vuota
    # Stop scatta a ogni turno: se le sorgenti non sono cambiate non si duplica lo snapshot
    sig_file = ctx.state_file("stop-snapshot-sig")
    sig = f"{_mtime(SPAWN_LOG)}|{len(spawns)}|{_mtime(EPHEMERAL_DIR) if has_ephemeral else 0}"
    try:
        if sig_file.read_text(encoding="utf-8") == sig:
            return None
    except OSError:
        pass
    try:
        sig_file.parent.mkdir(parents=True, exist_ok=True)
        sig_file.write_text(sig, encoding="utf-8")
    except OSError:
        pass
    sid = sig_file.parent.name
    try:
        snap = STOP_DIR / f"{sid}-{time.strftime('%Y%m%d-%H%M%S')}"
        snap.mkdir(parents=True, exist_ok=True)
        if spawns:
            (snap / "recent-spawns.log").write_text("\n".join(spawns) + "\n", encoding="utf-8", newline="\n")
        if has_ephemeral:
            import shutil
            shutil.copytree(EPHEMERAL_DIR, snap / "_ephemeral", dirs_exist_ok=True)
    except OSError:
        pass
    try:
        every = [p for p in STOP_DIR.iterdir() if p.is_dir()]
    except OSError:
        return None
    # Rotazione per sessione (il nome è `<sid>-YYYYmmdd-HHMMSS`), poi pulizia per età
    prefix = re.compile(re.escape(sid) + r"-\d{8}-\d{6}$")
    _rotate([p for p in every if prefix.match(p.name)])
    cutoff = time.time() - STOP_MAX_AGE_S
    for p in every:
        if 0 < _mtime(p) < cutoff:
            _remove(p)
    return None
