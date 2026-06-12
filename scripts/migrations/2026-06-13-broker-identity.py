#!/usr/bin/env python3
"""Broker bus-identity migration — ESC-002 decision (a+), DIR-002 T1b/T1c.

Enforces bus identity (from_agent, to_agent, kind, seq) on the HCOM broker:
  1. Dedupe: for each identity group with >1 row (seq IS NOT NULL), keep the
     lowest id, coalesce read_at = MAX(read_at) over the group, delete the rest.
     Every removed row is logged to stdout (id, ts, from, to, kind, seq,
     body length, md5) — the pre-migration .db backup is the recovery path.
  2. Constraint: partial UNIQUE index idx_messages_identity
     ON messages(from_agent, to_agent, kind, seq) WHERE seq IS NOT NULL.
     NULL-seq kinds (NUDGE/EVENT/TEST) stay unconstrained by design.

Idempotent: re-running on a migrated DB removes 0 rows and keeps the index.
Runs inside one BEGIN IMMEDIATE transaction (safe alongside live sessions;
sqlite's writer lock serializes, busy_timeout retries instead of forcing).

Usage: python3 2026-06-13-broker-identity.py <db_path> [--dry-run]
"""
import hashlib
import sqlite3
import sys


def migrate(db_path: str, dry_run: bool = False) -> int:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("BEGIN IMMEDIATE")
    try:
        before = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        groups = cur.execute(
            """
            SELECT from_agent, to_agent, kind, seq, COUNT(*) AS n,
                   MIN(id) AS keep_id, MAX(read_at) AS merged_read_at
            FROM messages
            WHERE seq IS NOT NULL
            GROUP BY from_agent, to_agent, kind, seq
            HAVING COUNT(*) > 1
            ORDER BY from_agent, to_agent, kind, seq
            """
        ).fetchall()

        removed = 0
        for g in groups:
            doomed = cur.execute(
                "SELECT id, ts, from_agent, to_agent, kind, seq, body"
                " FROM messages"
                " WHERE from_agent=? AND to_agent=? AND kind=? AND seq=? AND id != ?"
                " ORDER BY id",
                (g["from_agent"], g["to_agent"], g["kind"], g["seq"], g["keep_id"]),
            ).fetchall()
            for d in doomed:
                md5 = hashlib.md5(d["body"].encode("utf-8", "replace")).hexdigest()
                print(
                    f"REMOVE id={d['id']} ts={d['ts']}"
                    f" from={d['from_agent']} to={d['to_agent']}"
                    f" kind={d['kind']} seq={d['seq']}"
                    f" len={len(d['body'])} md5={md5}"
                    f" (keep id={g['keep_id']})"
                )
            cur.execute(
                "UPDATE messages SET read_at=? WHERE id=?",
                (g["merged_read_at"], g["keep_id"]),
            )
            cur.execute(
                "DELETE FROM messages"
                " WHERE from_agent=? AND to_agent=? AND kind=? AND seq=? AND id != ?",
                (g["from_agent"], g["to_agent"], g["kind"], g["seq"], g["keep_id"]),
            )
            removed += cur.rowcount

        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_identity"
            " ON messages(from_agent, to_agent, kind, seq)"
            " WHERE seq IS NOT NULL"
        )

        after = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        residual = cur.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE seq IS NOT NULL"
            " GROUP BY from_agent, to_agent, kind, seq HAVING COUNT(*) > 1)"
        ).fetchone()[0]

        if dry_run:
            cur.execute("ROLLBACK")
            verdict = "DRY-RUN (rolled back)"
        else:
            cur.execute("COMMIT")
            verdict = "COMMITTED"
        print(
            f"{verdict}: rows {before} -> {after} (removed {removed},"
            f" groups {len(groups)}); residual dup groups: {residual}"
        )
        return 0 if residual == 0 else 1
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(migrate(args[0], dry_run="--dry-run" in sys.argv))
