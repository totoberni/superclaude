#!/usr/bin/env python3
"""
hcom-broker.py — SQLite-backed message broker for HCOM (Hook Comms).

Replaces flat-file ~/.claude/comms/<orch-name>/ with durable + queryable comms.
Phase A: standalone library; Phase B: dual-write alongside flat-files; Phase C: SQLite-only.

Usage from Python:
    from hcom_broker import Broker
    b = Broker()
    msg_id = b.send("@orch-example", "DIR", seq=12, body="...", from_agent="meta")
    msgs = b.recv("orch-example", kinds=["DIR", "NUDGE"])

Usage from CLI (if invoked directly):
    python3 hcom-broker.py send --to @orch-example --kind DIR --seq 12 --body "..."
    python3 hcom-broker.py recv --self orch-example
    python3 hcom-broker.py status
"""

import sqlite3
import os
import time
import contextlib
import json
import sys
from typing import Optional, List, Dict, Any
from pathlib import Path

DB_PATH = os.path.expanduser("~/.claude/comms/.broker.db")


class Broker:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        if not os.path.exists(db_path):
            raise RuntimeError(
                f"HCOM DB not initialized: {db_path}. Run ~/.claude/scripts/hcom-init.sh first."
            )

    def _conn(self) -> sqlite3.Connection:
        # Use isolation_level=None for autocommit; we use explicit transactions.
        c = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
        return c

    # === messages ===

    def send(
        self,
        to_agent: str,
        kind: str,
        body: str,
        seq: Optional[int] = None,
        from_agent: Optional[str] = None,
    ) -> int:
        """Insert a message. Returns the new message id."""
        from_agent = from_agent or os.environ.get("CLAUDE_AGENT_NAME", "unknown")
        ts = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages (ts, from_agent, to_agent, kind, seq, body) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, from_agent, to_agent, kind, seq, body),
            )
            return cur.lastrowid

    def recv(
        self,
        self_agent: str,
        kinds: Optional[List[str]] = None,
        unread_only: bool = True,
        mark_read: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch messages addressed to self_agent (or @self_agent or *)."""
        targets = [self_agent, f"@{self_agent}", "*"]
        with self._conn() as c:
            placeholders = ",".join("?" * len(targets))
            sql = f"SELECT * FROM messages WHERE to_agent IN ({placeholders})"
            params: List[Any] = list(targets)

            if kinds:
                kp = ",".join("?" * len(kinds))
                sql += f" AND kind IN ({kp})"
                params.extend(kinds)

            if unread_only:
                sql += " AND read_at IS NULL"

            sql += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)

            rows = [dict(r) for r in c.execute(sql, params).fetchall()]

            if mark_read and rows:
                ids = [r["id"] for r in rows]
                ip = ",".join("?" * len(ids))
                c.execute(f"UPDATE messages SET read_at=? WHERE id IN ({ip})", [int(time.time())] + ids)

            return rows

    # === locks ===

    @contextlib.contextmanager
    def lock(self, path: str, agent: str, ttl_sec: int = 30):
        """Acquire a file_lock; raises on collision unless TTL expired."""
        path = os.path.abspath(path)
        with self._conn() as c:
            self._cleanup_expired_locks(c)
            now = int(time.time())
            try:
                c.execute(
                    "INSERT INTO file_locks (path, locked_by, acquired_at, ttl_sec) VALUES (?, ?, ?, ?)",
                    (path, agent, now, ttl_sec),
                )
            except sqlite3.IntegrityError:
                row = c.execute("SELECT locked_by, acquired_at FROM file_locks WHERE path=?", (path,)).fetchone()
                raise RuntimeError(
                    f"file_lock collision on {path}: held by {row['locked_by']} since {row['acquired_at']}"
                )
        try:
            yield
        finally:
            with self._conn() as c:
                c.execute("DELETE FROM file_locks WHERE path=? AND locked_by=?", (path, agent))

    def _cleanup_expired_locks(self, c: sqlite3.Connection):
        now = int(time.time())
        c.execute("DELETE FROM file_locks WHERE acquired_at + ttl_sec < ?", (now,))

    def cleanup_expired_locks(self):
        with self._conn() as c:
            self._cleanup_expired_locks(c)

    # === agent_status ===

    def mark_active(self, agent: str, state: str = "WORKING", pid: Optional[int] = None):
        pid = pid if pid is not None else os.getpid()
        now = int(time.time())
        with self._conn() as c:
            existing = c.execute("SELECT 1 FROM agent_status WHERE agent=?", (agent,)).fetchone()
            if existing:
                c.execute(
                    "UPDATE agent_status SET last_active_at=?, state=?, pid=? WHERE agent=?",
                    (now, state, pid, agent),
                )
            else:
                c.execute(
                    "INSERT INTO agent_status (agent, pid, started_at, last_active_at, state) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (agent, pid, now, now, state),
                )

    def status(self) -> Dict[str, Any]:
        """Return brief stats."""
        with self._conn() as c:
            return {
                "messages_total": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "messages_unread": c.execute("SELECT COUNT(*) FROM messages WHERE read_at IS NULL").fetchone()[0],
                "agents_tracked": c.execute("SELECT COUNT(*) FROM agent_status").fetchone()[0],
                "active_locks": c.execute("SELECT COUNT(*) FROM file_locks").fetchone()[0],
            }

    # === inspection helpers (read-only) ===

    def recent_messages(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return up to `limit` most-recent messages, newest first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def active_locks(self) -> List[Dict[str, Any]]:
        """Return all current file_locks, oldest-acquired first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM file_locks ORDER BY acquired_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def agent_table(self) -> List[Dict[str, Any]]:
        """Return all rows from agent_status, most-recently-active first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM agent_status ORDER BY last_active_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]


# === CLI entry ===
def _cli():
    import argparse
    p = argparse.ArgumentParser(description="HCOM broker CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send")
    s.add_argument("--to", required=True)
    s.add_argument("--kind", required=True)
    s.add_argument("--seq", type=int, default=None)
    s.add_argument("--body", required=True)
    s.add_argument("--from", dest="from_agent", default=None)

    r = sub.add_parser("recv")
    r.add_argument("--self", dest="self_agent", required=True)
    r.add_argument("--kinds", nargs="*", default=None)
    r.add_argument("--unread-only", action="store_true", default=True)
    r.add_argument("--limit", type=int, default=20)

    sub.add_parser("status")

    a = p.parse_args()
    b = Broker()

    if a.cmd == "send":
        mid = b.send(a.to, a.kind, a.body, seq=a.seq, from_agent=a.from_agent)
        print(json.dumps({"id": mid}))
    elif a.cmd == "recv":
        msgs = b.recv(a.self_agent, kinds=a.kinds, unread_only=a.unread_only, limit=a.limit)
        print(json.dumps(msgs, indent=2, default=str))
    elif a.cmd == "status":
        print(json.dumps(b.status(), indent=2))


if __name__ == "__main__":
    _cli()
