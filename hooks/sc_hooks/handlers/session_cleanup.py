"""session-cleanup: pulizia e manutenzione a fine sessione (SessionEnd).

In ordine, dal meno al più costoso (SessionEnd ha poco tempo a disposizione):
1. riga in `~/.claude/logs/session-history.log` (massimo 500 righe);
2. cancellazione della cartella di stato della sessione e dei file
   `~/.claude/tmp/<sid>-baseline.*`;
3. cancellazione delle cartelle di stato più vecchie di 7 giorni;
4. rotazione di `~/.claude/history.jsonl` oltre 10 MiB in
   `~/.claude/_backups/history/*.gz` (ne restano 5);
5. backup giornaliero del DB di memoria in `~/.claude/_backups/memory/` con
   `sqlite3.Connection.backup`, sicuro anche con scrittori concorrenti (ne restano 10).
Tutto è best-effort: ogni errore di filesystem o sqlite viene ignorato.
"""
from __future__ import annotations

import time
from pathlib import Path

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

HISTORY_LOG = core.LOG_DIR / "session-history.log"
HISTORY_JSONL = core.DATA_HOME / "history.jsonl"
HISTORY_BACKUP_DIR = core.DATA_HOME / "_backups" / "history"
MEMORY_DB = core.sc_profile.memory_db_path(core.PROFILE)
MEMORY_BACKUP_DIR = core.DATA_HOME / "_backups" / "memory"

HISTORY_LOG_LINES = 500
HISTORY_ROTATE_BYTES = 10 * 1024 * 1024
HISTORY_KEEP = 5
MEMORY_KEEP = 10
STATE_MAX_AGE_S = 7 * 86400


def _rmtree(p: Path) -> None:
    import shutil
    shutil.rmtree(p, ignore_errors=True)


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _session_dir(ctx: Ctx) -> Path | None:
    """Cartella di stato della sessione, solo se è davvero una figlia diretta di STATE_DIR.

    Un session id come `..` sopravvive alla sanificazione di `state_file`: senza
    questo controllo la cancellazione ricorsiva potrebbe risalire a `~/.claude`.
    """
    if not ctx.session_id:
        return None
    d = ctx.state_file("x").parent
    try:
        if d.name in ("", ".", "..") or d.resolve().parent != core.STATE_DIR.resolve():
            return None
    except OSError:
        return None
    return d


def _newest_mtime(d: Path) -> float:
    """mtime più recente tra la cartella e il suo contenuto diretto."""
    try:
        times = [d.stat().st_mtime]
        times += [c.stat().st_mtime for c in d.iterdir()]
    except OSError:
        return 0.0
    return max(times)


def _duration(d: Path | None) -> str:
    """Durata approssimata: dal marcatore più vecchio della cartella di stato a ora."""
    if d is None:
        return "?"
    try:
        times = [c.stat().st_mtime for c in d.iterdir()]
    except OSError:
        return "?"
    return f"{max(0, int(time.time() - min(times))) // 60}min" if times else "?"


def log_history(ctx: Ctx, duration: str) -> None:
    reason = str(ctx.payload.get("reason") or "unknown").replace("\n", " ").replace("\r", " ")
    line = (f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ended: agent={ctx.agent or 'bare'} "
            f"duration={duration} session={ctx.session_id[:8]} exit={reason}\n")
    try:
        HISTORY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_LOG.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
        lines = HISTORY_LOG.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if len(lines) > HISTORY_LOG_LINES:
            tmp = HISTORY_LOG.with_name(HISTORY_LOG.name + ".tmp")
            tmp.write_text("".join(lines[-HISTORY_LOG_LINES:]), encoding="utf-8", newline="\n")
            tmp.replace(HISTORY_LOG)
    except OSError:
        pass


def purge_old_state(max_age_s: float = STATE_MAX_AGE_S) -> None:
    cutoff = time.time() - max_age_s
    try:
        dirs = [d for d in core.STATE_DIR.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return
    for d in dirs:
        newest = _newest_mtime(d)
        if 0 < newest < cutoff:
            _rmtree(d)


def rotate_history_jsonl() -> None:
    try:
        if HISTORY_JSONL.stat().st_size <= HISTORY_ROTATE_BYTES:
            return
    except OSError:
        return
    import gzip
    import shutil
    archive = HISTORY_BACKUP_DIR / f"history-{time.strftime('%Y%m%d-%H%M%S')}.jsonl.gz"
    try:
        HISTORY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with HISTORY_JSONL.open("rb") as src, gzip.open(archive, "wb") as dst:
            shutil.copyfileobj(src, dst)
        # Troncamento sul posto: Claude Code continua ad accodare sullo stesso file
        with HISTORY_JSONL.open("r+b") as fh:
            fh.truncate(0)
    except OSError:
        _unlink(archive)                           # niente archivi duplicati se il troncamento fallisce
        return
    try:
        archives = sorted(HISTORY_BACKUP_DIR.glob("history-*.jsonl.gz"), key=lambda p: p.name, reverse=True)
    except OSError:
        return
    for old in archives[HISTORY_KEEP:]:
        _unlink(old)


def backup_memory_db() -> None:
    dest = MEMORY_BACKUP_DIR / f"memory-{time.strftime('%Y%m%d')}.db"
    try:
        if not MEMORY_DB.is_file() or dest.exists():
            return
        MEMORY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    import os
    import sqlite3
    part = dest.with_name(dest.name + ".part")
    _unlink(part)                                  # residuo di un backup interrotto
    src = dst = None
    ok = False
    try:
        src = sqlite3.connect(MEMORY_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        dst = sqlite3.connect(str(part), timeout=2)
        src.backup(dst)
        ok = True
    except (sqlite3.Error, OSError, ValueError):
        pass
    finally:
        for conn in (dst, src):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    if not ok:
        _unlink(part)
        return
    try:
        os.replace(part, dest)
    except OSError:
        _unlink(part)
        return
    try:
        backups = sorted(MEMORY_BACKUP_DIR.glob("memory-????????.db"), key=lambda p: p.name, reverse=True)
    except OSError:
        return
    for old in backups[MEMORY_KEEP:]:
        _unlink(old)


@on("SessionEnd", name="session-cleanup")
def handle(ctx: Ctx) -> Result | None:
    sdir = _session_dir(ctx)
    if ctx.session_id:
        log_history(ctx, _duration(sdir))
    if sdir is not None:
        _rmtree(sdir)
        try:
            for f in core.TMP_DIR.glob(f"{sdir.name}-baseline.*"):
                _unlink(f)
        except OSError:
            pass
    purge_old_state()
    rotate_history_jsonl()
    backup_memory_db()
    return None
