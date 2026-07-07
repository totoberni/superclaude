#!/usr/bin/env bash
# broker-queries.sh - shared HCOM broker SQL snippets.
#
# Extracted from the sqlite3 SELECT snippets duplicated (as concept, and for
# three of the four subcommands as near-verbatim SQL) across:
#   - skills/portfolio/SKILL.md     latest-RPT-per-orch query, mode --orch
#     (portfolio's own copy is fused into a larger combined query that also
#     computes an unanswered-ESC count in the same statement; the standalone
#     latest-RPT SQL below is sourced from status.md instead, see note below)
#   - skills/status/SKILL.md        latest-RPT-per-orch query, step 2
#     (source of the latest-rpt SQL body below)
#   - skills/super-health/SKILL.md  unanswered-ESC count, section "1g. Comms"
#     (adds a 30-minute grace threshold not present in the other two copies;
#     this script follows comms-query's fuller listing instead, see note below)
#   - skills/comms-query/SKILL.md   source of the unanswered-esc, orphan-dir,
#     and volume SQL bodies below, taken verbatim
#
# Drift note: unanswered-ESC is NOT byte-identical across the four skills.
# portfolio.md counts all read_at IS NULL rows (no time threshold);
# super-health.sh applies a 30-minute grace threshold; comms-query.md returns
# a full per-row listing with no threshold. This script implements the
# comms-query.md version (the one whose query is literally named
# "unanswered-esc"), matching the subcommand name required below.
#
# Read-only SELECTs against the HCOM broker only. Any other verb is refused.
#
# Usage:
#   broker-queries.sh latest-rpt [agent]
#   broker-queries.sh unanswered-esc
#   broker-queries.sh orphan-dir
#   broker-queries.sh volume <since>
#
# latest-rpt      Latest RPT per orch, optionally filtered to one agent.
# unanswered-esc  All ESC entries with read_at IS NULL, oldest first.
# orphan-dir      DIR entries with no matching RPT for the same orch and seq.
# volume <since>  Daily message volume since an ISO date (default 2026-04-01).

DB="$HOME/.claude/comms/.broker.db"

[ -f "$DB" ] || { echo "HCOM broker not initialized - Phase D requires SQLite. Run hcom-init.sh first."; exit 1; }

case "$1" in
  latest-rpt)
    AGENT="$2"
    if [ -n "$AGENT" ]; then
      # M1 fix (wf-skills review round 1): reject before any SQL runs (allowlist),
      # then defence-in-depth quote-double the survivor in case the allowlist ever
      # regresses. Both layers, not one.
      AGENT_RE='^[A-Za-z0-9_@.-]+$'
      if ! [[ "$AGENT" =~ $AGENT_RE ]]; then
        echo "broker-queries.sh: invalid agent name (allowed charset: A-Za-z0-9_@.-)" >&2
        exit 2
      fi
      AGENT_ESC="${AGENT//\'/\'\'}"
      WHERE_CLAUSE="kind='RPT' AND from_agent = '$AGENT_ESC'"
    else
      WHERE_CLAUSE="kind='RPT'"
    fi
    sqlite3 -header -column "$DB" "
      SELECT from_agent, seq, datetime(ts,'unixepoch') AS t, substr(body,1,80) AS preview
      FROM messages
      WHERE $WHERE_CLAUSE
      GROUP BY from_agent
      HAVING ts=MAX(ts)
      ORDER BY ts DESC;
    "
    ;;
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
  volume)
    SINCE="${2:-2026-04-01}"
    # M1 fix (wf-skills review round 1): reject before any SQL runs (allowlist),
    # then defence-in-depth quote-double the survivor in case the allowlist ever
    # regresses. Both layers, not one.
    SINCE_RE='^[A-Za-z0-9 :+._-]+$'
    if ! [[ "$SINCE" =~ $SINCE_RE ]]; then
      echo "broker-queries.sh: invalid since-arg (allowed charset: A-Za-z0-9 :+._-)" >&2
      exit 2
    fi
    SINCE_ESC="${SINCE//\'/\'\'}"
    sqlite3 -header -column "$DB" "
      SELECT
        date(ts, 'unixepoch') AS day,
        COUNT(*) AS msgs,
        SUM(CASE WHEN kind='DIR' THEN 1 ELSE 0 END) AS dirs,
        SUM(CASE WHEN kind='RPT' THEN 1 ELSE 0 END) AS rpts
      FROM messages
      WHERE ts >= strftime('%s', '$SINCE_ESC')
      GROUP BY day
      ORDER BY day DESC;
    "
    ;;
  *)
    echo "Usage: broker-queries.sh <latest-rpt [agent]|unanswered-esc|orphan-dir|volume <since>>"
    exit 1
    ;;
esac
