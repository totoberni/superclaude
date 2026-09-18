#!/usr/bin/env python3
"""spawn_log_summary.py - analyze ~/.claude/comms/_spawns.log

Usage: spawn_log_summary.py [--by-type|--by-parent|--recent] [<since>]
  <since> is an ISO-8601 prefix (e.g. 2026-05-09 or 2026-05-09T16) used as
  a string lower-bound filter on the timestamp column.
Referenced from: ~/.claude/plans/swarm-first-v2/validation-rubric.md

Port Python (solo stdlib) di spawn-log-summary.sh: stessa CLI e stesso output.
Il log e' TSV: timestamp, parent agent, subagent_type, poi campi liberi.
"""
import os
import sys
from collections import Counter

LOG = os.path.join(os.path.expanduser("~"), ".claude", "comms", "_spawns.log")


def read_rows(path, since=""):
    """Righe del log (senza newline) con timestamp >= since (confronto tra stringhe)."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        rows = [line.rstrip("\r\n") for line in fh]
    rows = [r for r in rows if r]
    if since:
        rows = [r for r in rows if r.split("\t", 1)[0] >= since]
    return rows


def _field(row, idx):
    parts = row.split("\t")
    return parts[idx] if len(parts) > idx else ""


def counts(rows, idx, limit=None):
    """Righe 'conteggio valore' ordinate per conteggio decrescente, come
    `sort | uniq -c | sort -rn` (a parita' di conteggio: ordine alfabetico)."""
    ranked = sorted(Counter(_field(r, idx) for r in rows).items(),
                    key=lambda kv: (-kv[1], kv[0]))
    if limit is not None:
        ranked = ranked[:limit]
    return ["%7d %s" % (n, name) for name, n in ranked]


def render(rows, mode):
    if mode == "--by-type":
        return counts(rows, 2)
    if mode == "--by-parent":
        return counts(rows, 1)
    if mode == "--recent":
        return rows[-20:]
    out = ["## Spawn Log Summary", "Total events: %d" % len(rows), "", "By subagent_type:"]
    out += counts(rows, 2, 10)
    out += ["", "By parent agent:"]
    out += counts(rows, 1, 10)
    out += ["", "Last 5 events:"]
    out += rows[-5:]
    return out


def main(argv=None, log_path=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log_path = log_path or LOG
    if not os.path.isfile(log_path):
        print("No spawn log found")
        return 0
    mode = argv[0] if argv else "summary"
    since = argv[1] if len(argv) > 1 else ""
    if len(argv) == 1 and mode[:1].isdigit():
        # Comodita' rispetto al bash: un solo argomento che sembra una data e' <since>.
        mode, since = "summary", argv[0]
    for line in render(read_rows(log_path, since), mode):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
