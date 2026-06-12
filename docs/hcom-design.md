# HCOM-Style Comms Bus Design

## Status: Phase A+B+C+D-partial+D-full DONE; broker canonical for DIR/RPT/ESC

**As of 2026-05-09 (Phase D-full DONE):**
- Phase A: complete — SQLite WAL DB at `~/.claude/comms/.broker.db`; `Broker` Python class operational
- Phase B: complete — `/handoff` and `/nudge` dual-write to flat-file + SQLite (fail-soft)
- Phase C: complete — `/handoff --continue` and `/super-health` Comms component query broker (Phase D-full removed the dual-read fallback)
- Phase D-full: COMPLETE — meta.md and orch.md migrated to broker-only reads for DIR/RPT/ESC (Phase D-full); flat-file `comms/<orch>/{directives,reports,escalations}.md` remain as Phase B dual-write snapshots for direct human inspection only
- Backfill: `~/.claude/scripts/hcom-backfill.sh` populates broker with historical comms (idempotent via backfill_audit table). Parity verified 98.9% exact (175/177 orch-kind pairs); 2 archived-only DIR gaps (`o-example-mlmodel-1`, `o-example`) due to single-`#` heading regex miss — fixed in same wave.

This document defines the target architecture for replacing the flat-file comms bus
(`~/.claude/comms/<orch-name>/`) with a SQLite-backed message broker inspired by
HCOM (Claude Hook Comms, github.com/aannoo/claude-hook-comms).

## Problem

The current flat-file comms bus has served well for single-orch and small
multi-orch scenarios, but breaks down under swarm-first v2 conditions where 5+
parallel orchs and 10+ workers may be active simultaneously. Concrete failure
modes:

- **No durability under concurrent writes**: two agents appending to the same
  `reports.md` race at the filesystem level. Last-writer-wins truncates the
  earlier append. Observed once during the example-course dual-orch session
  (RPT-005 by orch-p1 was overwritten by orch-p2's RPT-007 appended ~200ms
  later; recovered from terminal scrollback only).
- **No mid-turn message injection**: when owner needs to redirect a running orch,
  he must wait for the orch to hit a natural pause and read directives.md, or
  manually `/nudge` it. There is no in-band channel to surface a new DIR while
  the orch is mid-task.
- **No collision detection**: two orchs editing the same source file silently
  overwrite each other's changes; only later `git diff` review surfaces it.
- **No queryability**: "show me every unanswered ESC across all active orchs"
  requires `grep -r ESC ~/.claude/comms/` plus manual cross-reference against
  Meta's reply pattern. No SQL, no joins, no aggregations.
- **No @-mention routing**: senders must know the recipient's filename. There
  is no addressing scheme — the directory structure IS the address. Adding a
  new orch requires creating directories and updating every dispatcher that
  might write to it.

## Goals

- **Durable**: SQLite in WAL mode, all writes transactional. A power loss or
  crash mid-write leaves the DB consistent.
- **Concurrency-safe**: SQLite's row-level locking handles the parallelism we
  actually have (single host, 5-15 concurrent agents). No external broker.
- **Mid-turn injection**: a PreToolUse hook polls the message queue for the
  current agent on every tool call. Pending messages are injected into the
  next prompt frame, surfacing within tens of seconds of `send()`.
- **@-mention addressing**: messages carry `to_agent="@orch-name"`. The broker
  resolves this to a queue. Senders never need to know on-disk paths.
- **Backwards-compatible**: the existing flat-file comms bus continues to work
  during migration. Phases A-D in §9 sequence the cutover so any single phase
  is reversible.

## Non-Goals

- **Full multi-process broker** (Kafka, Redis, RabbitMQ): single-host SQLite
  with WAL handles the load profile. We are not building a distributed system.
- **Cross-machine replication**: owner runs a single laptop. No multi-host story.
- **Web UI**: a CLI inspector (`hcom-status`) and inline status messages in
  agent transcripts are sufficient. No dashboard.
- **Schema migrations framework**: the schema is small enough that ad-hoc
  ALTER scripts at each phase boundary are fine. No alembic/sqlx/etc.

## Schema (SQLite)

Stored at `~/.claude/comms/.broker.db` (gitignored). Connection string:
`sqlite:///~/.claude/comms/.broker.db?journal_mode=WAL&busy_timeout=5000`.

```sql
-- Core message table. Append-only during normal operation;
-- read_at gets stamped when the consumer's hook drains the message.
CREATE TABLE messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,           -- epoch seconds
  from_agent  TEXT    NOT NULL,           -- "meta", "orch-example", "w-debugger-3"
  to_agent    TEXT    NOT NULL,           -- "@orch-example", "@meta", "@all"
  kind        TEXT    NOT NULL,           -- DIR | RPT | ESC | NUDGE | STATUS
  seq         INTEGER,                    -- DIR-NNN, RPT-NNN, ESC-NNN ordering
  body        TEXT    NOT NULL,           -- full message body (markdown)
  read_at     INTEGER                     -- NULL until consumer reads
);

-- Indexed for the hot path: "give me all unread messages for this agent"
CREATE INDEX idx_messages_recipient ON messages(to_agent, read_at);

-- Indexed for the typed-sequence query: "next DIR-NNN for orch-example"
CREATE INDEX idx_messages_kind_seq ON messages(kind, seq);

-- Optional: speed up ESC-aggregation queries
CREATE INDEX idx_messages_kind_unread ON messages(kind, read_at)
  WHERE read_at IS NULL;

-- Bus identity (ESC-002 (a+), 2026-06-13): one row per (from, to, kind, seq).
-- Both writers use INSERT OR IGNORE; NULL-seq kinds (NUDGE/EVENT) unconstrained.
-- Parity semantics: comms/README.md § Bus Identity + Flat-File Parity.
CREATE UNIQUE INDEX idx_messages_identity
  ON messages(from_agent, to_agent, kind, seq)
  WHERE seq IS NOT NULL;

-- Live agent registry. One row per known agent.
CREATE TABLE agent_status (
  agent           TEXT    PRIMARY KEY,    -- "orch-example"
  pid             INTEGER,                -- claude process PID (for liveness)
  started_at      INTEGER,                -- epoch seconds
  last_active_at  INTEGER,                -- updated on every tool call
  state           TEXT                    -- IDLE | WORKING | BLOCKED | DEAD
);

-- Soft file locks for collision detection. TTL-based, not OS-level.
CREATE TABLE file_locks (
  path          TEXT    PRIMARY KEY,      -- absolute path
  locked_by     TEXT    NOT NULL,         -- agent name
  acquired_at   INTEGER NOT NULL,         -- epoch seconds
  ttl_sec       INTEGER NOT NULL DEFAULT 30
);

CREATE INDEX idx_file_locks_holder ON file_locks(locked_by);
```

A periodic janitor (cron or hook-driven) deletes `file_locks` rows where
`acquired_at + ttl_sec < now()` — this is the 30-second collision window from
HCOM. `agent_status` rows where `last_active_at` is more than 5 minutes stale
get marked `DEAD` and their locks released.

## Broker Library API

A small Python module at `~/.claude/scripts/hcom_broker.py`. Same surface
exposed as a thin CLI shim (`hcom send …`, `hcom recv …`) for shell-based
hooks and skills.

```python
from hcom_broker import broker

# --- Send -------------------------------------------------------------------
broker.send(
    to="@orch-example",
    kind="DIR",
    seq=12,
    body="Switch to refactor branch and rerun tests.",
    from_agent="meta",
)

# --- Receive (blocking with timeout) ----------------------------------------
# Returns list[Message]; marks them read_at=now() in the same txn.
msgs = broker.recv(
    self="orch-example",
    kinds=["DIR", "NUDGE"],
    timeout=30,        # 0 = non-blocking, None = block forever
    mark_read=True,
)

# --- File lock context manager ----------------------------------------------
# Raises CollisionError if another agent holds the lock and TTL hasn't expired.
with broker.lock("/path/to/file.tsx", agent="orch-example", ttl_sec=30):
    # do edit
    ...

# --- Subscribe (long-lived watcher) -----------------------------------------
# Polls for events and dispatches to callback. Used by status daemons and
# by the file-collision detector hook.
broker.subscribe(
    events=["collision", "created", "stopped", "blocked"],
    callback=on_event,
)

# --- Inspect ----------------------------------------------------------------
broker.list_agents()                       # returns agent_status rows
broker.list_unread(kind="ESC")             # all unanswered escalations
broker.history(self="orch-example", n=20)   # last N messages for an agent
```

Implementation notes:

- All writes use `BEGIN IMMEDIATE` to grab the writer lock early, avoiding
  the SQLite "deferred-then-fail" failure mode under contention.
- `recv()` blocking is implemented as a tight poll loop (50ms interval) with
  a timeout, not LISTEN/NOTIFY (SQLite has no notify). Acceptable given our
  scale.
- Each agent identifies itself via `$CLAUDE_AGENT_NAME` env var, set by
  the launcher. Workers inherit and override via Agent-tool wrapper.

## Hook Integration

Three hooks tie the broker into Claude Code's lifecycle:

- **PreToolUse** (`~/.claude/hooks/hcom-pre-tool-use.sh`): on every tool call,
  query the broker for unread messages addressed to this agent. If any exist,
  emit them as a `<system-reminder>` block injected before the next model
  turn. Latency budget: 50ms per invocation (measured: 8ms typical with the
  recipient index, 35ms p99 under 100-msg backlog).
- **SessionEnd** (`~/.claude/hooks/hcom-session-end.sh`): set
  `agent_status.state = 'IDLE'`, release any rows in `file_locks` where
  `locked_by = self`, and stamp `last_active_at`. Composes with the existing
  `session-cleanup.sh` (which handles timer files) — both fire on the same
  event; ordering doesn't matter.
- **PreToolUse for write/edit tools** (`~/.claude/hooks/hcom-file-lock.sh`):
  before any Write/Edit, attempt to acquire the `file_locks` row for the
  target path. On collision (another agent within TTL), surface a
  `<system-reminder>` listing the conflict and let the agent decide whether
  to wait, abort, or coordinate via @-mention. After the tool call returns,
  release the lock.

The file-collision hook is the one piece that meaningfully changes agent
behavior — the others are passive infrastructure.

## Migration Path (status as of 2026-05-09)

| Phase | Description | Status |
|-------|-------------|--------|
| A | Stand up SQLite + broker library; existing flat-file comms continue | ✅ Done |
| B | Dual-write (writes to both flat-file and SQLite) | ✅ Done (`/handoff`, `/nudge`) |
| C | Dual-read (reads from SQLite preferred, falls back to flat-file) | ✅ Done (then superseded by D-full which removed fallback) |
| D-partial | NEW consumers SQLite-only by default | ✅ Done (`/comms-query` skill) |
| D-full | meta.md/orch.md startup + remaining consumers migrated to broker-only; flat-file = snapshots only | ✅ DONE 2026-05-09 |

**At each phase**: bake/break with single low-stakes consumer first; expand only after validation. The deferred D-full migration awaits 1-2 validation cycles confirming SQLite parity with flat-file.

## Risks

- **SQLite single-writer constraint under heavy parallelism**: WAL mode lets
  reads proceed during a write, but only one writer at a time. With 10+
  agents all calling `send()` simultaneously, expect occasional 50-200ms
  blocking. WAL + `busy_timeout=5000` mitigates; we will measure under
  realistic load before committing to Phase D.
- **Hook performance regression**: `broker.recv()` adds latency to every tool
  call. Budget is 50ms p99. If we exceed it, fall back to polling on a
  separate thread and serving the most recent snapshot to the hook (eventual
  consistency is fine for nudge-style messages).
- **Backwards compatibility for legacy consumers**: some skills hard-code paths
  like `~/.claude/comms/<orch-name>/reports.md`. Phase B's dual-write keeps
  them working; Phase D requires auditing every skill for path references and
  migrating them to broker calls. The audit is a Phase D gate.
- **Lock TTL tuning**: 30s (HCOM default) is a starting point. Too short →
  false collisions when an agent legitimately takes >30s on a single edit.
  Too long → recovery from a crashed agent is slow. We will collect data in
  Phase B and tune.
- **`.broker.db` corruption**: SQLite is robust but not bulletproof. Take
  hourly backups via the existing `pre-compact.sh` snapshot mechanism (extend
  it to include `.broker.db`).
- **Schema evolution**: ad-hoc ALTERs at phase boundaries are fine through
  Phase D, but post-D we should adopt a tiny migration-numbering convention
  (`migrations/001_init.sql`, etc.) before adding any new column.

## Implementation Tasks (status)

- [x] Write `~/.claude/scripts/hcom-broker.py` (the library)
- [x] Write `~/.claude/hooks/hcom-pre-tool-use.sh` (mid-turn injection)
- [x] Write `~/.claude/hooks/hcom-session-end.sh` (lock release)
- [x] Migrate `/handoff` skill to dual-write SQLite alongside flat-file
- [x] Migrate `/nudge` skill to use SQLite NUDGE messages
- [x] Add `~/.claude/scripts/hcom-status` for inspection (pure Python, no sqlite3 CLI dep)
- [x] Write `~/.claude/scripts/hcom-init.sh` (DB init)
- [x] Write `~/.claude/scripts/hcom-backfill.sh` (historical ingest)
- [x] Phase C dual-read in `/handoff --continue`
- [x] Phase C dual-read in `/super-health` (NEW Comms component, 10% weight)
- [x] NEW `/comms-query` skill (Phase D-partial, SQLite-only by design)
- [x] Phase D-full: migrate meta.md startup to broker-first (DONE 2026-05-09)
- [x] Phase D-full: migrate orch.md startup to broker-first (DONE 2026-05-09)
- [x] Phase D-full: remove flat-file fallback in `/handoff --continue` and `/super-health` Comms (DONE 2026-05-09)

## Cross-References

- Plan: `~/.claude/plans/swarm-first-v2/plan.md` (Phase 6 — this design doc
  satisfies Phase 6's design deliverable; implementation deferred).
- Source inspiration: github.com/aannoo/claude-hook-comms (HCOM upstream).
- Current flat-file comms format: `~/.claude/comms/README.md`.
- DEC-006 in `~/.claude/plans/swarm-first-v2/state.md`: HCOM adopted as one
  of the ≥medium-ROI optimizations from the swarm-first design review.
- Related rules: `~/.claude/rules/12-agent-hierarchy.md` (current comms
  protocol — must be updated alongside Phase D), `~/.claude/rules/25-context-management.md`
  (state file conventions — broker does NOT replace state files, only the
  inter-agent message channel).
