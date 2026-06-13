#!/usr/bin/env python3
# hcom_backfill.py — parse one comms Markdown file and idempotently backfill
# the HCOM SQLite broker.  Invoked by hcom-backfill.sh; also importable.
#
# Dedup oracle = the bus itself (ESC-002 decision (a+)): bus identity is
# (from_agent, to_agent, kind, seq), enforced by the partial UNIQUE index
# idx_messages_identity (WHERE seq IS NOT NULL). Inserts are INSERT OR
# IGNORE; an ignored row means the identity is already on the bus (e.g. a
# condensed direct-send) and counts as skipped_existing. When skipped AND
# md5(body) differs from the bus row's, the entry is flagged divergent_body
# — surfaced, never silently absorbed. Divergence is expected and tolerated:
# the flat file remains the full-body SOT.
#
# backfill_audit is a pure log of backfill-applied rows; it is NOT consulted
# for dedup (the old ikey-oracle never saw direct-sends — see ESC-002).
#
# argv: <file_path> <orch> <kind> <from_agent> <to_agent> <db_path> <mode>
#   mode: "apply" | "dry-run"

import sys
import re
import os
import hashlib
import sqlite3
import time
from datetime import datetime


def _body_md5(body):
    return hashlib.md5(body.encode('utf-8', 'replace')).hexdigest()


def _bare(name):
    """Bare agent names are canonical on the bus (DIR-003, owner-ratified)."""
    return name[1:] if name and name.startswith('@') and len(name) > 1 else name


def process_file(file_path, orch, kind, from_agent, to_agent, db_path, mode):
    """Parse *file_path* for entries of *kind* (DIR/RPT/ESC) and backfill.

    Agent names are normalized to bare spelling before insert/lookup, so the
    derived identity always matches direct-sends (which also emit bare).

    Returns a tuple (applied, skipped_existing, divergent, would, total_matches).
    Prints a one-line summary (plus one line per divergent body) to stdout.
    """
    orch = _bare(orch)
    from_agent = _bare(from_agent)
    to_agent = _bare(to_agent)
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Match entries: ## <KIND>-NNN with optional flexible suffix
    pat = re.compile(
        r'^#{1,2} (' + re.escape(kind) + r'-\d+(?:-[A-Za-z0-9-]+)?)(.*?)$',
        re.MULTILINE,
    )
    matches = list(pat.finditer(content))

    conn = sqlite3.connect(db_path, timeout=10.0)
    cur = conn.cursor()

    applied = 0
    skipped_existing = 0
    divergent = 0
    would = 0

    for i, m in enumerate(matches):
        header = m.group(1)
        seq_match = re.search(r'-(\d+)', header)
        seq = int(seq_match.group(1)) if seq_match else None

        # Body = text after this header up to the next header (or EOF)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()

        if not body:
            continue

        # Extract Time field if present
        ts_match = re.search(r'\*\*Time\*\*:\s*([^\n]+)', body)
        ts = None
        if ts_match:
            ts_str = ts_match.group(1).strip().rstrip(',')
            candidates = [
                ts_str,
                ts_str.replace(' UTC', '').replace(' UK', '')
                      .replace(' BST', '').replace(' GMT', '').strip(),
            ]
            for c in candidates:
                try:
                    dt = datetime.fromisoformat(c.replace('Z', '+00:00'))
                    ts = int(dt.timestamp())
                    break
                except Exception:
                    pass
                try:
                    dt = datetime.strptime(c, '%Y-%m-%d %H:%M')
                    ts = int(dt.timestamp())
                    break
                except Exception:
                    pass
                try:
                    dt = datetime.strptime(c, '%Y-%m-%d %H:%M:%S')
                    ts = int(dt.timestamp())
                    break
                except Exception:
                    pass
        if ts is None:
            ts = int(os.path.getmtime(file_path))

        flat_md5 = _body_md5(body)

        def _bus_row():
            """Existing bus row for this identity (None if seq is NULL-scoped)."""
            if seq is None:
                return None
            return cur.execute(
                "SELECT id, body FROM messages"
                " WHERE from_agent=? AND to_agent=? AND kind=? AND seq=?",
                (from_agent, to_agent, kind, seq),
            ).fetchone()

        def _report_divergence(row):
            bus_md5 = _body_md5(row[1])
            if bus_md5 != flat_md5:
                print(
                    f"    DIVERGENT-BODY {header}: bus id={row[0]}"
                    f" bus_md5={bus_md5} bus_len={len(row[1])}"
                    f" flat_md5={flat_md5} flat_len={len(body)}"
                    f" (flat file is the full-body SOT)"
                )
                return 1
            return 0

        if mode == "apply":
            cur.execute(
                "INSERT OR IGNORE INTO messages"
                " (ts, from_agent, to_agent, kind, seq, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, from_agent, to_agent, kind, seq, body),
            )
            if cur.rowcount == 0:
                skipped_existing += 1
                row = _bus_row()
                if row is not None:
                    divergent += _report_divergence(row)
            else:
                # backfill_audit: pure log of backfill-applied rows only
                ikey = f"{orch}|{kind}|{seq}|{hashlib.md5(body[:200].encode('utf-8')).hexdigest()}"
                cur.execute(
                    "INSERT OR IGNORE INTO backfill_audit (ikey, inserted_at)"
                    " VALUES (?, ?)",
                    (ikey, int(time.time())),
                )
                applied += 1
        else:  # dry-run: advisory existence check against the bus
            row = _bus_row()
            if row is not None:
                skipped_existing += 1
                divergent += _report_divergence(row)
            else:
                would += 1

    conn.commit()
    conn.close()
    print(
        f"  {os.path.basename(file_path)}: kind={kind}"
        f" entries={len(matches)} applied={applied}"
        f" skipped_existing={skipped_existing}"
        f" divergent_body={divergent} would_apply={would}"
    )
    return applied, skipped_existing, divergent, would, len(matches)


if __name__ == "__main__":
    if len(sys.argv) != 8:
        print(
            "Usage: hcom_backfill.py <file> <orch> <kind>"
            " <from_agent> <to_agent> <db_path> <mode>",
            file=sys.stderr,
        )
        sys.exit(2)
    file_path, orch, kind, from_agent, to_agent, db_path, mode = sys.argv[1:8]
    process_file(file_path, orch, kind, from_agent, to_agent, db_path, mode)
