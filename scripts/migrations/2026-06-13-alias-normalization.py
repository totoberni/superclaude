#!/usr/bin/env python3
"""Broker alias-spelling normalization — DIR-003 T1.

Ratified convention (owner, 2026-06-13): **bare agent names are canonical** on
the HCOM bus. This one-time migration strips the leading '@' from agent-name
columns across all spelling-bearing broker tables:

  messages.from_agent / messages.to_agent
      The partial UNIQUE index idx_messages_identity is LIVE — normalizing an
      '@'-spelled row can collide with an existing bare-spelled row of the
      same (from, to, kind, seq) identity. Collisions resolve via the DIR-002
      dedupe rule (shared broker_dedupe.coalesce_rows): keep lowest id,
      coalesce read_at = MAX over the group, delete the rest, log removals.
      NULL-seq rows are unconstrained and update directly.
  agent_status.agent
      PK — on collision with an existing bare row, merge (earliest
      started_at; the more recently active row wins last_active_at/state/pid)
      and drop the '@' row.
  file_locks.locked_by
      Not unique — straight update.

backfill_audit.ikey already uses bare comms-dir names (verified 2026-06-13:
0 rows contain '@') and is a pure log — left untouched. Only a single leading
'@' is stripped; alias VARIANTS (dated/decommissioned suffixes) are kept
as-is per DIR-002's pitfall note (variant unification is a separate concern).

Idempotent: a second run finds no '@'-spelled rows and changes nothing.
Runs inside one BEGIN IMMEDIATE transaction (busy_timeout retries alongside
live sessions); finishes with PRAGMA integrity_check.

Usage: python3 2026-06-13-alias-normalization.py <db_path> [--dry-run]
"""
import sqlite3
import sys

from broker_dedupe import coalesce_rows


def _bare(name):
    """Strip ONE leading '@' (never reduce a name to the empty string)."""
    return name[1:] if name and name.startswith("@") and len(name) > 1 else name


def migrate(db_path: str, dry_run: bool = False) -> int:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("BEGIN IMMEDIATE")
    try:
        before = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        # --- messages ---
        at_rows = cur.execute(
            "SELECT id FROM messages"
            " WHERE from_agent LIKE '@_%' OR to_agent LIKE '@_%' ORDER BY id"
        ).fetchall()
        normalized = 0
        removed = 0
        for r in at_rows:
            row = cur.execute(
                "SELECT * FROM messages WHERE id=?", (r["id"],)
            ).fetchone()
            if row is None:
                continue  # already deleted by an earlier coalesce
            nf, nt = _bare(row["from_agent"]), _bare(row["to_agent"])
            if row["seq"] is not None:
                # All rows whose POST-normalization identity is (nf, nt, kind, seq)
                group = [
                    g["id"]
                    for g in cur.execute(
                        "SELECT id FROM messages WHERE kind=? AND seq=?"
                        " AND from_agent IN (?, ?) AND to_agent IN (?, ?)"
                        " ORDER BY id",
                        (row["kind"], row["seq"], nf, "@" + nf, nt, "@" + nt),
                    ).fetchall()
                ]
                keep_id, n = coalesce_rows(cur, group)
                removed += n
            else:
                keep_id = row["id"]
            cur.execute(
                "UPDATE messages SET from_agent=?, to_agent=? WHERE id=?"
                " AND (from_agent != ? OR to_agent != ?)",
                (nf, nt, keep_id, nf, nt),
            )
            normalized += cur.rowcount

        # --- agent_status (PK = agent) ---
        status_merged = 0
        for a in cur.execute(
            "SELECT * FROM agent_status WHERE agent LIKE '@_%'"
        ).fetchall():
            bare = _bare(a["agent"])
            ex = cur.execute(
                "SELECT * FROM agent_status WHERE agent=?", (bare,)
            ).fetchone()
            if ex is not None:
                winner = a if a["last_active_at"] >= ex["last_active_at"] else ex
                cur.execute(
                    "UPDATE agent_status SET started_at=?, last_active_at=?,"
                    " state=?, pid=? WHERE agent=?",
                    (
                        min(a["started_at"], ex["started_at"]),
                        winner["last_active_at"],
                        winner["state"],
                        winner["pid"],
                        bare,
                    ),
                )
                cur.execute("DELETE FROM agent_status WHERE agent=?", (a["agent"],))
                print(f"MERGE agent_status {a['agent']} -> {bare}")
            else:
                cur.execute(
                    "UPDATE agent_status SET agent=? WHERE agent=?",
                    (bare, a["agent"]),
                )
            status_merged += 1

        # --- file_locks (locked_by is not unique) ---
        cur.execute(
            "UPDATE file_locks SET locked_by=substr(locked_by, 2)"
            " WHERE locked_by LIKE '@_%'"
        )
        locks_fixed = cur.rowcount

        after = cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        residual = cur.execute(
            "SELECT (SELECT COUNT(*) FROM messages"
            "          WHERE from_agent LIKE '@_%' OR to_agent LIKE '@_%')"
            " + (SELECT COUNT(*) FROM agent_status WHERE agent LIKE '@_%')"
            " + (SELECT COUNT(*) FROM file_locks WHERE locked_by LIKE '@_%')"
        ).fetchone()[0]
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]

        if dry_run:
            cur.execute("ROLLBACK")
            verdict = "DRY-RUN (rolled back)"
        else:
            cur.execute("COMMIT")
            verdict = "COMMITTED"
        print(
            f"{verdict}: messages {before} -> {after}"
            f" (normalized {normalized}, removed {removed} via dedupe);"
            f" agent_status fixed {status_merged}; file_locks fixed {locks_fixed};"
            f" residual @-rows: {residual}; integrity: {integrity}"
        )
        return 0 if residual == 0 and integrity == "ok" else 1
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
