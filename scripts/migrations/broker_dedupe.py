#!/usr/bin/env python3
"""Shared dedupe primitive for HCOM broker migrations (rules/20 SOT discipline).

coalesce_rows implements the DIR-002 dedupe rule for a group of `messages`
rows that share (or are about to share) one bus identity
(from_agent, to_agent, kind, seq): keep the lowest id, coalesce
read_at = MAX(read_at) over the group, delete the rest, and log every removed
row (id, ts, from, to, kind, seq, body length, md5) — the pre-migration .db
backup is the recovery path (no quarantine table).

Used by:
  2026-06-13-broker-identity.py    (exact-identity duplicate groups)
  2026-06-13-alias-normalization.py (@-spelled vs bare collision groups)
"""
import hashlib


def coalesce_rows(cur, ids, log=print):
    """Coalesce the `messages` rows in *ids* into the lowest-id survivor.

    *cur* must be a cursor inside an open transaction, on a connection with
    row_factory = sqlite3.Row. Returns (keep_id, removed_count).
    """
    ids = sorted(ids)
    keep_id = ids[0]
    doomed = ids[1:]
    if not doomed:
        return keep_id, 0

    all_ph = ",".join("?" * len(ids))
    merged_read_at = cur.execute(
        f"SELECT MAX(read_at) FROM messages WHERE id IN ({all_ph})", ids
    ).fetchone()[0]

    doomed_ph = ",".join("?" * len(doomed))
    for d in cur.execute(
        "SELECT id, ts, from_agent, to_agent, kind, seq, body FROM messages"
        f" WHERE id IN ({doomed_ph}) ORDER BY id",
        doomed,
    ).fetchall():
        md5 = hashlib.md5(d["body"].encode("utf-8", "replace")).hexdigest()
        log(
            f"REMOVE id={d['id']} ts={d['ts']}"
            f" from={d['from_agent']} to={d['to_agent']}"
            f" kind={d['kind']} seq={d['seq']}"
            f" len={len(d['body'])} md5={md5}"
            f" (keep id={keep_id})"
        )

    cur.execute("UPDATE messages SET read_at=? WHERE id=?", (merged_read_at, keep_id))
    cur.execute(f"DELETE FROM messages WHERE id IN ({doomed_ph})", doomed)
    return keep_id, len(doomed)
