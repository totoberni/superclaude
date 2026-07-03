"""Per-automation sqlite store: ledger, blacklist, domain memory, queue, counter.

One `store.db` per automation (plan 7.4, WT-3), toto-only and never synced. This
module owns every table and all raw persistence; the higher layers (discover,
queue_sm, questionnaire) call these methods and hold no SQL of their own
(separation of concerns, rules/15 #7).

v1 uses stdlib sqlite3 only. Domain memories are FTS5-searchable when the build
has FTS5 (verified at connect time); otherwise a LIKE fallback keeps search
working without a heavy dependency. vec0 vector search (7.4) is a toto-side
follow-up (WT-8) and is deliberately out of the fixtures-only v1.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  identity_key TEXT PRIMARY KEY,
  item_id      TEXT UNIQUE,
  vendor       TEXT,
  company_slug TEXT,
  title        TEXT,
  url          TEXT,
  status       TEXT NOT NULL,
  score        INTEGER,
  first_seen   TEXT,
  last_seen    TEXT
);
CREATE TABLE IF NOT EXISTS blacklist (
  item_id      TEXT PRIMARY KEY,
  identity_key TEXT,
  reason       TEXT,
  created_at   TEXT
);
CREATE TABLE IF NOT EXISTS domain_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  topic      TEXT,
  body       TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS queue (
  item_id      TEXT PRIMARY KEY,
  identity_key TEXT,
  state        TEXT NOT NULL,
  prev_state   TEXT,
  score        INTEGER,
  visible      INTEGER NOT NULL DEFAULT 1,
  channel      TEXT,
  payload      TEXT,
  updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS questionnaire (
  q_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id    TEXT,
  field_path TEXT NOT NULL,
  prompt     TEXT NOT NULL,
  priority   TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS fetch_cache (
  url           TEXT PRIMARY KEY,
  etag          TEXT,
  last_modified TEXT,
  body          TEXT,
  fetched_at    TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self.fts_enabled = _detect_fts5(self._conn)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        if self.fts_enabled:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS domain_memory_fts "
                "USING fts5(body, topic, content='domain_memory', content_rowid='id')"
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- monotonic id counter (WT-10: short, stable, never re-used) ----------
    def next_counter(self) -> int:
        cur = self._conn.execute("SELECT value FROM meta WHERE key='id_counter'")
        row = cur.fetchone()
        current = int(row["value"]) if row else 0
        nxt = current + 1
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('id_counter', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(nxt),),
        )
        self._conn.commit()
        return nxt

    # -- ledger (structural no-repeat) ---------------------------------------
    def is_known(self, identity_key: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM ledger WHERE identity_key=? "
            "UNION SELECT 1 FROM blacklist WHERE identity_key=?",
            (identity_key, identity_key),
        )
        return cur.fetchone() is not None

    def record_ledger(self, identity_key: str, item_id: str, vendor: str,
                      company_slug: str, title: str, url: str, status: str,
                      score: int) -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO ledger(identity_key, item_id, vendor, company_slug, "
            "title, url, status, score, first_seen, last_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(identity_key) DO UPDATE SET status=excluded.status, "
            "score=excluded.score, last_seen=excluded.last_seen",
            (identity_key, item_id, vendor, company_slug, title, url,
             status, score, now, now),
        )
        self._conn.commit()

    def set_ledger_status(self, identity_key: str, status: str) -> None:
        self._conn.execute(
            "UPDATE ledger SET status=?, last_seen=? WHERE identity_key=?",
            (status, _now(), identity_key),
        )
        self._conn.commit()

    # -- blacklist (manual, sticky) ------------------------------------------
    def blacklist_add(self, item_id: str, identity_key: str, reason: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO blacklist(item_id, identity_key, reason, "
            "created_at) VALUES(?,?,?,?)",
            (item_id, identity_key, reason, _now()),
        )
        self._conn.commit()

    # -- domain memory (FTS5 searchable, LIKE fallback) ----------------------
    def add_domain_memory(self, topic: str, body: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO domain_memory(topic, body, created_at) VALUES(?,?,?)",
            (topic, body, _now()),
        )
        row_id = cur.lastrowid
        if self.fts_enabled:
            self._conn.execute(
                "INSERT INTO domain_memory_fts(rowid, body, topic) VALUES(?,?,?)",
                (row_id, body, topic),
            )
        self._conn.commit()
        return row_id

    def search_domain_memory(self, query: str) -> list[str]:
        if self.fts_enabled:
            cur = self._conn.execute(
                "SELECT dm.body FROM domain_memory_fts f "
                "JOIN domain_memory dm ON dm.id = f.rowid "
                "WHERE domain_memory_fts MATCH ? ORDER BY rank",
                (query,),
            )
        else:
            cur = self._conn.execute(
                "SELECT body FROM domain_memory WHERE body LIKE ?",
                (f"%{query}%",),
            )
        return [row["body"] for row in cur.fetchall()]

    # -- queue rows ----------------------------------------------------------
    def upsert_queue(self, item_id: str, identity_key: str, state: str,
                    prev_state: str | None, score: int, visible: int,
                    channel: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO queue(item_id, identity_key, state, prev_state, score, "
            "visible, channel, payload, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET state=excluded.state, "
            "prev_state=excluded.prev_state, score=excluded.score, "
            "visible=excluded.visible, channel=excluded.channel, "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            (item_id, identity_key, state, prev_state, score, visible,
             channel, json.dumps(payload), _now()),
        )
        self._conn.commit()

    def bulk_update_scores(self, updates) -> int:
        """Rescore many queue rows (score + payload) and their ledger score in
        ONE transaction with a single commit.

        `updates` is an iterable of (item_id, identity_key, score, payload_dict).
        The naive per-row commit path took 67 minutes on ~7900 rows; batching the
        whole rescore into one transaction is the load-bearing performance fix.
        On any error the whole batch rolls back (all-or-nothing).
        """
        now = _now()
        count = 0
        try:
            for item_id, identity_key, score, payload in updates:
                self._conn.execute(
                    "UPDATE queue SET score=?, payload=?, updated_at=? "
                    "WHERE item_id=?",
                    (score, json.dumps(payload), now, item_id),
                )
                self._conn.execute(
                    "UPDATE ledger SET score=?, last_seen=? WHERE identity_key=?",
                    (score, now, identity_key),
                )
                count += 1
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return count

    def get_queue_row(self, item_id: str) -> dict | None:
        cur = self._conn.execute("SELECT * FROM queue WHERE item_id=?", (item_id,))
        row = cur.fetchone()
        return _queue_row_to_dict(row) if row else None

    def all_queue_rows(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM queue ORDER BY score DESC")
        return [_queue_row_to_dict(r) for r in cur.fetchall()]

    def held_count(self) -> int:
        # Held backlog = demoted items only. Blacklisted items are also visible=0
        # but are not backlog, so the state filter keeps this in step with the
        # digest's own held count (notify.render_digest). Board-absent (payload
        # closed=true, set by close_absent) rows are excluded too: they are no
        # longer a demoted backlog awaiting promotion, they are gone for good
        # (w-reviewer HIGH).
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM queue WHERE visible=0 AND state='demoted' "
            "AND COALESCE(json_extract(payload,'$.closed'),0)=0"
        )
        return int(cur.fetchone()["n"])

    # -- questionnaire items -------------------------------------------------
    def add_questionnaire(self, item_id: str | None, field_path: str,
                         prompt: str, priority: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO questionnaire(item_id, field_path, prompt, priority, "
            "status, created_at) VALUES(?,?,?,?, 'open', ?)",
            (item_id, field_path, prompt, priority, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def resolve_questionnaire(self, q_id: int) -> None:
        self._conn.execute(
            "UPDATE questionnaire SET status='answered' WHERE q_id=?", (q_id,)
        )
        self._conn.commit()

    # -- conditional-GET cache (W4 3.4; keyed by absolute request URL) --------
    def get_fetch_cache(self, url: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT url, etag, last_modified, body, fetched_at "
            "FROM fetch_cache WHERE url=?",
            (url,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def set_fetch_cache(self, url: str, etag: str | None,
                        last_modified: str | None, body: str) -> None:
        self._conn.execute(
            "INSERT INTO fetch_cache(url, etag, last_modified, body, fetched_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, body=excluded.body, "
            "fetched_at=excluded.fetched_at",
            (url, etag, last_modified, body, _now()),
        )
        self._conn.commit()

    # -- board-presence liveness (W4 3.4; R-WT-8 invariant 4) ----------------
    def close_absent(self, vendor: str, slug: str,
                     present_identity_keys: set[str]) -> list[str]:
        """Close queue items for vendor+slug no longer present on the board.

        7.5 has no `closed` queue state, so absence is modelled without inventing
        one: the ledger row is marked `closed`, the queue row is hidden
        (visible=0) with its state untouched, and a payload `closed` flag lets the
        digest exclude it (notify.render_digest). Only pending_review and demoted
        rows are eligible, so awaiting_input / blacklisted / submitted are spared.
        The caller is responsible for the critical guard: pass only sources whose
        fetch succeeded this run, since a failed fetch is unavailability, not
        absence.
        """
        cur = self._conn.execute(
            "SELECT q.item_id, q.identity_key, q.payload FROM queue q "
            "JOIN ledger l ON l.identity_key = q.identity_key "
            "WHERE l.vendor=? AND l.company_slug=? "
            "AND q.state IN ('pending_review', 'demoted')",
            (vendor, slug),
        )
        rows = cur.fetchall()
        closed: list[str] = []
        for row in rows:
            if row["identity_key"] in present_identity_keys:
                continue
            payload = json.loads(row["payload"]) if row["payload"] else {}
            payload["closed"] = True
            now = _now()
            self._conn.execute(
                "UPDATE queue SET visible=0, payload=?, updated_at=? "
                "WHERE item_id=?",
                (json.dumps(payload), now, row["item_id"]),
            )
            self._conn.execute(
                "UPDATE ledger SET status='closed', last_seen=? "
                "WHERE identity_key=?",
                (now, row["identity_key"]),
            )
            closed.append(row["item_id"])
        self._conn.commit()
        return closed


def _detect_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _queue_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["payload"] = json.loads(data["payload"]) if data["payload"] else {}
    return data
