"""v3 Comms search store — hybrid-search + HTML over the HCOM message bus.

A SEPARATE, lightweight search store that gives broker comms messages the same
hybrid keyword (FTS5 BM25) + semantic (sqlite-vec KNN) search and lazy-HTML
capability the memory DB has — WITHOUT touching the hot-path message bus.

Two databases are involved:
  - BROKER_DB (~/.claude/comms/.broker.db): the HCOM msg-bus. Opened READ-ONLY
    here; never written. Ground-truth source of messages.
  - COMMS_DB  (~/.claude/comms/.comms.db): this search store. Built by syncing
    forward-only from the broker.

This module is a THIN adapter over memory_db.py: it does NOT re-implement
embedding, FTS5, sqlite-vec, RRF, render, or schema. memory_db's init_db /
upsert / search / get_html all take a `db_path`; we point them at COMMS_DB.
The shared `memories` table (the table name is an internal detail of the reused
memory_db schema) holds one row per broker message, keyed by path `broker/<id>`.

Forward-only, embed-all, HTML-on-demand:
  - Every broker message is embedded + FTS-indexed on sync (embed-all).
  - HTML is rendered lazily via get_html (HTML-on-demand).
  - Sync advances by ascending broker `id` watermark (forward-only); the broker
    `id` is the monotonic global PK. `seq` is NOT globally monotonic and may be
    NULL, so it is never used as the watermark. `read_at` (operational state) is
    never synced — it stays in the broker.

Always run under ~/.claude/.venv/bin/python with HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1 (fastembed + sqlite-vec live there; model cached at
~/.claude/.cache/fastembed).

CLI: init | sync [--rebuild] | search <query> [-k N] [--mode fts|vec|hybrid] |
     html <id> | stats
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# memory_db.py is a sibling in this directory. Insert the dir on sys.path BEFORE
# importing it so `import memory_db` resolves regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_db  # noqa: E402  (deliberate post-sys.path import)

# --- Constants ---------------------------------------------------------------

COMMS_DB = Path.home() / ".claude" / "comms" / ".comms.db"
BROKER_DB = Path.home() / ".claude" / "comms" / ".broker.db"

TIER = "comms"

# --- Field mapping helpers ---------------------------------------------------


def _subject(from_agent: str, to_agent: str, body: str) -> str:
    """Build the row `description`: `from→to: <first body line>`, ≤160 chars.

    The body line is the first non-empty, stripped line of the message body.
    The whole composed string is truncated to 160 chars.
    """
    line = ""
    for candidate in (body or "").splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    return f"{from_agent}→{to_agent}: {line}"[:160]


def _name(kind: str, seq, msg_id: int) -> str:
    """Row `name`: `<kind>-<seq>` when seq present, else `<kind>-<id>`."""
    if seq is not None:
        return f"{kind}-{seq}"
    return f"{kind}-{msg_id}"


# --- Public API --------------------------------------------------------------


def init_db(db_path: Path | str = COMMS_DB) -> None:
    """Create the comms search-store schema. Delegates to memory_db.init_db.

    Idempotent.
    """
    memory_db.init_db(db_path)


def _watermark(comms_db: Path | str) -> int:
    """Highest synced broker id (0 if the store is empty / has no broker rows).

    Derived from the `broker/<id>` path keys already in the store, so the
    watermark survives independently of any broker state.
    """
    conn = memory_db._connect(comms_db)
    try:
        rows = conn.execute(
            "SELECT path FROM memories WHERE path LIKE 'broker/%'"
        ).fetchall()
    finally:
        conn.close()
    hi = 0
    for (path,) in rows:
        try:
            hi = max(hi, int(path.split("/")[1]))
        except (IndexError, ValueError):
            continue
    return hi


def _total_rows(comms_db: Path | str) -> int:
    """Count broker-sourced rows currently in the store."""
    conn = memory_db._connect(comms_db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memories WHERE path LIKE 'broker/%'"
        ).fetchone()[0]
    finally:
        conn.close()


def sync_from_broker(
    broker: Path | str = BROKER_DB,
    comms_db: Path | str = COMMS_DB,
    rebuild: bool = False,
) -> dict:
    """Forward-only sync of new broker messages into the comms search store.

    Ensures the schema exists, computes the id watermark (0 when `rebuild`),
    reads broker rows with id > watermark READ-ONLY, and upserts each per the
    field mapping. Idempotent: with no new messages, no rows are touched and no
    re-embedding occurs (memory_db.upsert short-circuits on unchanged content).

    Returns {synced, watermark_before, watermark_after, total_rows}.
    """
    init_db(comms_db)

    w0 = 0 if rebuild else _watermark(comms_db)

    src = sqlite3.connect(f"file:{broker}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            "SELECT id, ts, from_agent, to_agent, kind, seq, body "
            "FROM messages WHERE id > ? ORDER BY id",
            (w0,),
        ).fetchall()
    finally:
        src.close()

    synced = 0
    w1 = w0
    for r in rows:
        msg_id = r["id"]
        kind = r["kind"]
        from_agent = r["from_agent"]
        to_agent = r["to_agent"]
        seq = r["seq"]
        body = r["body"]
        memory_db.upsert(
            path=f"broker/{msg_id}",
            tier=TIER,
            agent=from_agent,
            type=kind,
            name=_name(kind, seq, msg_id),
            description=_subject(from_agent, to_agent, body),
            text=body,
            db_path=comms_db,
        )
        synced += 1
        if msg_id > w1:
            w1 = msg_id

    return {
        "synced": synced,
        "watermark_before": w0,
        "watermark_after": w1,
        "total_rows": _total_rows(comms_db),
    }


def search(query: str, k: int = 8, mode: str = "hybrid") -> list[dict]:
    """Hybrid/keyword/semantic search over the comms store.

    Delegates to memory_db.search against COMMS_DB. mode ∈ {fts, vec, hybrid}.
    """
    return memory_db.search(query, k, mode, COMMS_DB)


def get_html(id: int) -> str | None:
    """Lazily render + cache the HTML for one comms row. Delegates to memory_db."""
    return memory_db.get_html(id, COMMS_DB)


def stats(comms_db: Path | str = COMMS_DB) -> dict:
    """Counts proving embed-all coverage, plus kind breakdown and watermark.

    Returns {rows, fts_rows, vec_rows, kinds:{...}, watermark}. rows counts only
    broker-sourced rows; fts_rows/vec_rows count the shared index tables (the
    comms store holds only broker rows, so these should equal `rows`).
    """
    conn = memory_db._connect(comms_db)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE path LIKE 'broker/%'"
        ).fetchone()[0]
        fts_rows = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        vec_rows = conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
        kinds: dict[str, int] = {}
        for kind, n in conn.execute(
            "SELECT type, COUNT(*) FROM memories WHERE path LIKE 'broker/%' "
            "GROUP BY type ORDER BY COUNT(*) DESC"
        ).fetchall():
            kinds[kind] = n
    finally:
        conn.close()
    return {
        "rows": rows,
        "fts_rows": fts_rows,
        "vec_rows": vec_rows,
        "kinds": kinds,
        "watermark": _watermark(comms_db),
    }


# --- CLI ---------------------------------------------------------------------


def _db_exists() -> bool:
    return COMMS_DB.exists()


def _cmd_init(_args) -> int:
    init_db()
    print(f"init_db ok → {COMMS_DB}")
    return 0


def _cmd_sync(args) -> int:
    if not BROKER_DB.exists():
        print(f"error: broker not found at {BROKER_DB}", file=sys.stderr)
        return 1
    result = sync_from_broker(rebuild=args.rebuild)
    print(
        f"synced {result['synced']} message(s)  "
        f"(watermark {result['watermark_before']} → {result['watermark_after']})  "
        f"total_rows={result['total_rows']}"
    )
    if args.rebuild:
        print("  mode: rebuild (re-synced from watermark 0)")
    return 0


def _cmd_search(args) -> int:
    if not _db_exists():
        print(
            f"comms DB not found at {COMMS_DB} — run `sync` first.",
            file=sys.stderr,
        )
        return 1
    hits = search(args.query, k=args.k, mode=args.mode)
    if not hits:
        print(f"no results for {args.query!r} (mode={args.mode}).")
        return 0  # empty results are not an error
    print(f"top {len(hits)} (mode={args.mode}) for {args.query!r}:")
    for rank, h in enumerate(hits, 1):
        print(
            f"{rank:>2}. [{h['id']}] {h.get('name', '')}  "
            f"score={h.get('score', 0.0):.4f}"
        )
        desc = (h.get("description") or "").strip()
        if desc:
            print(f"     {desc}")
    return 0


def _cmd_html(args) -> int:
    if not _db_exists():
        print(
            f"comms DB not found at {COMMS_DB} — run `sync` first.",
            file=sys.stderr,
        )
        return 1
    out = get_html(args.id)
    if out is None:
        print(f"no comms row with id={args.id}.", file=sys.stderr)
        return 1
    print(out)
    return 0


def _cmd_stats(_args) -> int:
    if not _db_exists():
        print(
            f"comms DB not found at {COMMS_DB} — run `sync` first.",
            file=sys.stderr,
        )
        return 1
    s = stats()
    print(f"comms store: {COMMS_DB}")
    print(f"  rows      : {s['rows']}")
    print(f"  fts_rows  : {s['fts_rows']}")
    print(f"  vec_rows  : {s['vec_rows']}")
    print(f"  watermark : {s['watermark']}")
    print("  kinds     :")
    for kind, n in s["kinds"].items():
        print(f"    {kind:<8} {n}")
    return 0


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="comms_db",
        description="Comms search store over the HCOM broker (read-only source).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_init = sub.add_parser("init", help="create the comms search-store schema")
    sp_init.set_defaults(func=_cmd_init)

    sp_sync = sub.add_parser("sync", help="forward-only sync new broker messages")
    sp_sync.add_argument(
        "--rebuild",
        action="store_true",
        help="re-sync every broker message from watermark 0 (no destructive drop)",
    )
    sp_sync.set_defaults(func=_cmd_sync)

    sp_search = sub.add_parser("search", help="hybrid/keyword/semantic search")
    sp_search.add_argument("query")
    sp_search.add_argument("-k", type=int, default=8, help="number of hits (default 8)")
    sp_search.add_argument(
        "--mode",
        choices=("fts", "vec", "hybrid"),
        default="hybrid",
        help="search mode (default hybrid)",
    )
    sp_search.set_defaults(func=_cmd_search)

    sp_html = sub.add_parser("html", help="render + print HTML for one comms row")
    sp_html.add_argument("id", type=int)
    sp_html.set_defaults(func=_cmd_html)

    sp_stats = sub.add_parser("stats", help="row/index counts + kind breakdown")
    sp_stats.set_defaults(func=_cmd_stats)

    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
