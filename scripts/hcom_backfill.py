#!/usr/bin/env python3
# hcom_backfill.py — parse one comms Markdown file and idempotently backfill
# the HCOM SQLite broker.  Invoked by hcom-backfill.sh; also importable.
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


def process_file(file_path, orch, kind, from_agent, to_agent, db_path, mode):
    """Parse *file_path* for entries of *kind* (DIR/RPT/ESC) and backfill.

    Returns a tuple (inserted, skipped, would, total_matches).
    Prints a one-line summary to stdout.
    """
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

    inserted = 0
    skipped = 0
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

        # Idempotency key
        body_hash = hashlib.md5(body[:200].encode('utf-8')).hexdigest()
        ikey = f"{orch}|{kind}|{seq}|{body_hash}"

        existing = cur.execute(
            "SELECT 1 FROM backfill_audit WHERE ikey=?", (ikey,)
        ).fetchone()
        if existing:
            skipped += 1
            continue

        if mode == "apply":
            cur.execute(
                "INSERT INTO messages (ts, from_agent, to_agent, kind, seq, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, from_agent, to_agent, kind, seq, body),
            )
            cur.execute(
                "INSERT INTO backfill_audit (ikey, inserted_at) VALUES (?, ?)",
                (ikey, int(time.time())),
            )
            inserted += 1
        else:
            would += 1

    conn.commit()
    conn.close()
    print(
        f"  {os.path.basename(file_path)}: kind={kind}"
        f" entries={len(matches)} inserted={inserted}"
        f" skipped={skipped} would={would}"
    )
    return inserted, skipped, would, len(matches)


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
