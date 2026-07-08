---
name: comms-query
description: "Use when running ad-hoc SQLite queries against the HCOM broker"
model: haiku
category: comms
user-invocable: true
argument-hint: "<query-name> [args...]"
allowed-tools: Read, Bash, Grep
---

# Comms Query (HCOM Phase D)

SQLite-only queries against `~/.claude/comms/.broker.db`. NO flat-file fallback — by design. This skill showcases the queryability HCOM enables that flat-file scans can't.

## Available Queries

| Query name | What it answers |
|------------|-----------------|
| `unanswered-esc` | All ESC entries with read_at IS NULL, sorted by age |
| `stuck-orks` | Orks whose latest activity (any kind) is >4h old + still in active comms (not archived) |
| `active-week` | Top N orks by message count in last 7 days |
| `dir-rpt-latency` | Average minutes between DIR-N and RPT-N per orch (catches slow orchs) |
| `orphan-dir` | DIR entries with no matching RPT (for any seq + orch) — directives that never resolved |
| `recent <agent>` | Last 20 messages addressed to or from <agent> |
| `volume <since>` | Daily message volume since <ISO date> |
| `--list` | List all available queries (this table) |
| `--sql <stmt>` | Run an arbitrary read-only SELECT (advanced; for ad-hoc exploration) |

## Procedure

### Step 1: Verify broker
```bash
DB="$HOME/.claude/comms/.broker.db"
[ -f "$DB" ] || { echo "HCOM broker not initialized — Phase D requires SQLite. Run hcom-init.sh first."; exit 1; }
```

### Step 2: Dispatch query

```bash
case "$1" in
  unanswered-esc)
    sqlite3 -header -column "$DB" "
      SELECT
        id,
        substr(from_agent, 1, 20) AS orch,
        seq,
        datetime(ts, 'unixepoch') AS sent,
        CAST((strftime('%s', 'now') - ts) / 60 AS INTEGER) AS age_min,
        substr(body, 1, 60) AS preview
      FROM messages
      WHERE kind='ESC' AND read_at IS NULL
      ORDER BY ts ASC;
    "
    ;;
  stuck-orks)
    THRESHOLD_MIN=${2:-240}
    sqlite3 -header -column "$DB" "
      SELECT
        CASE WHEN to_agent LIKE '@%' THEN substr(to_agent, 2) ELSE from_agent END AS orch,
        MAX(ts) AS last_ts,
        datetime(MAX(ts), 'unixepoch') AS last_active,
        CAST((strftime('%s', 'now') - MAX(ts)) / 60 AS INTEGER) AS idle_min
      FROM messages
      GROUP BY orch
      HAVING idle_min > $THRESHOLD_MIN
      ORDER BY idle_min DESC;
    "
    ;;
  active-week)
    LIMIT=${2:-10}
    sqlite3 -header -column "$DB" "
      SELECT
        CASE WHEN to_agent LIKE '@%' THEN substr(to_agent, 2) ELSE from_agent END AS orch,
        COUNT(*) AS msg_count,
        SUM(CASE WHEN kind='DIR' THEN 1 ELSE 0 END) AS dirs,
        SUM(CASE WHEN kind='RPT' THEN 1 ELSE 0 END) AS rpts,
        SUM(CASE WHEN kind='ESC' THEN 1 ELSE 0 END) AS escs
      FROM messages
      WHERE ts > strftime('%s', 'now') - (7 * 86400)
      GROUP BY orch
      ORDER BY msg_count DESC
      LIMIT $LIMIT;
    "
    ;;
  dir-rpt-latency)
    sqlite3 -header -column "$DB" "
      SELECT
        substr(d.to_agent, 2) AS orch,
        d.seq AS dir_seq,
        datetime(d.ts, 'unixepoch') AS dir_sent,
        datetime(r.ts, 'unixepoch') AS rpt_received,
        CAST((r.ts - d.ts) / 60 AS INTEGER) AS latency_min
      FROM messages d
      JOIN messages r
        ON r.kind='RPT'
       AND r.from_agent = substr(d.to_agent, 2)
       AND r.seq = d.seq
      WHERE d.kind='DIR'
      ORDER BY d.ts DESC
      LIMIT 30;
    "
    ;;
  orphan-dir)
    sqlite3 -header -column "$DB" "
      SELECT
        substr(d.to_agent, 2) AS orch,
        d.seq AS dir_seq,
        datetime(d.ts, 'unixepoch') AS sent,
        CAST((strftime('%s', 'now') - d.ts) / 60 AS INTEGER) AS age_min,
        substr(d.body, 1, 50) AS preview
      FROM messages d
      WHERE d.kind='DIR'
        AND NOT EXISTS (
          SELECT 1 FROM messages r
          WHERE r.kind='RPT'
            AND r.from_agent = substr(d.to_agent, 2)
            AND r.seq = d.seq
        )
      ORDER BY d.ts DESC;
    "
    ;;
  recent)
    AGENT="$2"
    [ -z "$AGENT" ] && { echo "Usage: comms-query recent <agent>"; exit 1; }
    sqlite3 -header -column "$DB" "
      SELECT id, datetime(ts, 'unixepoch') AS time, kind, seq, from_agent, to_agent, substr(body, 1, 50) AS preview
      FROM messages
      WHERE from_agent = '$AGENT' OR to_agent = '$AGENT' OR to_agent = '@$AGENT'
      ORDER BY ts DESC
      LIMIT 20;
    "
    ;;
  volume)
    SINCE="${2:-2026-04-01}"
    sqlite3 -header -column "$DB" "
      SELECT
        date(ts, 'unixepoch') AS day,
        COUNT(*) AS msgs,
        SUM(CASE WHEN kind='DIR' THEN 1 ELSE 0 END) AS dirs,
        SUM(CASE WHEN kind='RPT' THEN 1 ELSE 0 END) AS rpts
      FROM messages
      WHERE ts >= strftime('%s', '$SINCE')
      GROUP BY day
      ORDER BY day DESC;
    "
    ;;
  --list)
    grep -E '^\| \`' "$0" | head -10
    ;;
  --sql)
    SQL="$2"
    # Safety: only allow SELECT statements
    case "$SQL" in
      SELECT*|select*) sqlite3 -header -column "$DB" "$SQL" ;;
      *) echo "Error: --sql allows SELECT only (read-only)"; exit 1 ;;
    esac
    ;;
  *)
    echo "Usage: comms-query <query-name>"
    echo "Run 'comms-query --list' for available queries"
    exit 1
    ;;
esac
```

## Phase D Discipline (this skill)

- NO flat-file fallback. If broker unavailable, query exits with informative error.
- All queries are READ-ONLY (no INSERT/UPDATE/DELETE).
- `--sql` mode rejects any non-SELECT statement.

## When To Use

- Investigating "what happened recently?" across multiple orks
- Spotting stuck/orphan work that flat-file scan misses
- Pre-flight before commissioning new orks (volume + active-week show capacity)
- Debugging swarm-dispatch wave outcomes (recent + dir-rpt-latency)

## Cross-References

- HCOM design: `~/.claude/docs/hcom-design.md` § Migration Path (Phase D)
- Broker: `~/.claude/scripts/hcom-broker.py`
- Backfill: `~/.claude/scripts/hcom-backfill.sh` (run before this skill is useful for historical queries)
- Sister skill: `/swarm-status` (live in-flight workers; this skill is for COMMS history)
