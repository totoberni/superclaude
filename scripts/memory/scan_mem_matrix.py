"""Scan the agent-memory DB and emit per-tier health signals + DB integrity.

Usage: scpy memory/scan_mem_matrix.py [--budgets] [--json]
Default: human-readable tab-separated table (same as --budgets).

Port Python di scripts/scan-mem-matrix.sh: stesso formato di output, stessi flag
e stesse variabili d'ambiente (MEM_OVERSIZE_BYTES, MEM_STALE_DAYS, MEMORY_DB_PATH).
Usa solo il modulo `sqlite3` della libreria standard su una connessione di sola
lettura: niente CLI sqlite3, niente awk, niente estensione vec.

Segnali emessi (uno per tier, piu' una riga TOTAL e una riga INTEGRITY):
  ROWS     righe nel tier
  BYTES    somma di LENGTH(text)
  BIGGEST  LENGTH(text) massimo
  FAT      righe con LENGTH(text) > OVERSIZE_BYTES
  STALE    righe con `updated` piu' vecchio di STALE_DAYS giorni

Avvertenze (ereditate dall'originale):
  - STALE e' informativo solo dopo scritture organiche: subito dopo una
    migrazione in blocco tutte le righe condividono lo stesso `updated`.
  - `memories_vec` e' una tabella virtuale vec0: senza l'estensione non si puo'
    contare, quindi se ne verifica solo la PRESENZA in sqlite_master. La tripla
    autorevole memories == fts == vec resta `mem stats`.

Fail-safe: DB mancante o illeggibile -> una sola riga di skip, exit 0.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_OVERSIZE_BYTES = 8000
DEFAULT_STALE_DAYS = 60


def env_int(name: str, default: int) -> int:
    """Legge un intero dall'ambiente; valore assente o non valido -> default."""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def db_path() -> Path:
    """Percorso del DB: MEMORY_DB_PATH, altrimenti ~/.claude/agent-memory/.memory.db."""
    override = os.environ.get("MEMORY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "agent-memory" / ".memory.db"


def now_expr() -> str:
    """Istante di riferimento per la staleness.

    Di norma 'now' (come l'originale bash). MEM_NOW (ISO 8601) lo fissa: serve
    solo a rendere deterministici i test.
    """
    return os.environ.get("MEM_NOW", "").strip() or "now"


def open_ro(path: Path) -> tuple[sqlite3.Connection | None, str]:
    """Apre il DB in sola lettura. Restituisce (conn, "") oppure (None, motivo)."""
    if not path.is_file():
        return None, f"DB not built ({path})"
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
        # Forza la lettura dell'header: un file non SQLite fallisce qui.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.Error:
        return None, "invalid SQLite"
    return conn, ""


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    """Esegue una query; su qualunque errore restituisce [] (come `q()` in bash)."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Primo valore della prima riga come intero; 0 se assente o non numerico."""
    rows = q(conn, sql, params)
    try:
        return int(rows[0][0])
    except (IndexError, TypeError, ValueError):
        return 0


def _aggregate_sql(select_head: str, tail: str) -> str:
    return (
        f"SELECT {select_head}, COUNT(*), COALESCE(SUM(LENGTH(text)),0), "
        "COALESCE(MAX(LENGTH(text)),0), "
        "COALESCE(SUM(CASE WHEN LENGTH(text) > ? THEN 1 ELSE 0 END),0), "
        "COALESCE(SUM(CASE WHEN updated < datetime(?, ?) THEN 1 ELSE 0 END),0) "
        f"FROM memories {tail}"
    )


def tier_rows(conn: sqlite3.Connection, oversize: int, stale_days: int) -> list[tuple]:
    """Aggregati per tier, dal piu' popolato (a parita', ordine alfabetico)."""
    return q(
        conn,
        _aggregate_sql("tier", "GROUP BY tier ORDER BY COUNT(*) DESC, tier"),
        (oversize, now_expr(), f"-{stale_days} days"),
    )


def total_row(conn: sqlite3.Connection, oversize: int, stale_days: int) -> list[tuple]:
    return q(
        conn,
        _aggregate_sql("'TOTAL'", ""),
        (oversize, now_expr(), f"-{stale_days} days"),
    )


def integrity(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """(righe, documenti indicizzati FTS, presenza di memories_vec)."""
    rows = scalar(conn, "SELECT COUNT(*) FROM memories")
    idx = scalar(conn, "SELECT COUNT(*) FROM memories_fts_docsize")
    vec = scalar(conn, "SELECT COUNT(*) FROM sqlite_master WHERE name='memories_vec'")
    return rows, idx, vec


def table_lines(path: Path | None = None) -> list[str]:
    """Righe della tabella leggibile (senza newline finale)."""
    path = db_path() if path is None else path
    oversize = env_int("MEM_OVERSIZE_BYTES", DEFAULT_OVERSIZE_BYTES)
    stale_days = env_int("MEM_STALE_DAYS", DEFAULT_STALE_DAYS)
    out = [f"TIER\tROWS\tBYTES\tBIGGEST\tFAT(>{oversize})\tSTALE(>{stale_days}d)"]
    conn, reason = open_ro(path)
    if conn is None:
        out.append(f"(skip: {reason})\t0\t0\t0\t0\t0")
        return out
    try:
        for tier, *nums in tier_rows(conn, oversize, stale_days):
            out.append("\t".join([tier if tier else "(null)", *map(str, nums)]))
        for row in total_row(conn, oversize, stale_days):
            out.append("\t".join(map(str, row)))
        rows, idx, vec = integrity(conn)
        if idx == rows and vec >= 1:
            out.append(f"INTEGRITY\trows={rows}\tfts_idx={idx}\tvec=present\tOK\t-")
        else:
            vtxt = "present" if vec >= 1 else "MISSING"
            out.append(f"INTEGRITY\trows={rows}\tfts_idx={idx}\tvec={vtxt}\tDESYNC\t-")
    finally:
        conn.close()
    return out


def json_text(path: Path | None = None) -> str:
    """Stesso documento JSON compatto dell'originale (senza newline finale)."""
    path = db_path() if path is None else path
    conn, reason = open_ro(path)
    if conn is None:
        doc: dict = {"available": False, "reason": reason, "tiers": []}
        return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    oversize = env_int("MEM_OVERSIZE_BYTES", DEFAULT_OVERSIZE_BYTES)
    stale_days = env_int("MEM_STALE_DAYS", DEFAULT_STALE_DAYS)
    try:
        rows, idx, vec = integrity(conn)
        total_bytes = scalar(conn, "SELECT COALESCE(SUM(LENGTH(text)),0) FROM memories")
        tiers = [
            {"tier": t, "rows": n, "bytes": b, "biggest": big, "fat": fat, "stale": stale}
            for t, n, b, big, fat, stale in tier_rows(conn, oversize, stale_days)
            if t  # come l'originale: il tier NULL/vuoto non entra nel JSON
        ]
    finally:
        conn.close()
    doc = {
        "available": True,
        "oversize_bytes": oversize,
        "stale_days": stale_days,
        "integrity": {
            "rows": rows,
            "fts_index": idx,
            "vec_present": vec,
            "cohesive": idx == rows and vec >= 1,
        },
        "total": {"rows": rows, "bytes": total_bytes},
        "tiers": tiers,
    }
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


def _unix_newlines() -> None:
    """Output con `\\n` anche su Windows, come lo script bash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str]) -> int:
    _unix_newlines()
    fmt = argv[0] if argv else "table"
    if fmt in ("--budgets", "table"):
        sys.stdout.write("\n".join(table_lines()) + "\n")
        return 0
    if fmt == "--json":
        sys.stdout.write(json_text())
        return 0
    print(f"Unknown format: {fmt}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
