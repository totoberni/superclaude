#!/usr/bin/env bash
# hcom-session-end.sh
# Event: SessionEnd
# Purpose: release file_locks held by this agent + mark agent IDLE in agent_status.
# Phase A: opt-in (no-op if HCOM not initialized).
# Non-blocking.
#
# FAIL-SAFE: exit 0 ALWAYS. NEVER `set -e` — SessionEnd cleanup must never abort
# on a transient sqlite non-zero; every DB write below is guarded with `|| true`.
set -uo pipefail

DB_PATH="$HOME/.claude/comms/.broker.db"
[ -f "$DB_PATH" ] || exit 0
command -v sqlite3 >/dev/null 2>&1 || exit 0

input=$(cat 2>/dev/null || true)
session_id=$(echo "$input" | jq -r '.session_id // ""' 2>/dev/null)

# Resolve agent name (env or timer file)
agent_name="${CLAUDE_AGENT_NAME:-}"
if [ -z "$agent_name" ] && [ -n "$session_id" ]; then
  agent_file="$HOME/.claude/session-timers/${session_id}.agent"
  [ -f "$agent_file" ] && agent_name=$(cat "$agent_file" 2>/dev/null)
fi
[ -z "$agent_name" ] && exit 0

# Release locks held by this agent
sqlite3 "$DB_PATH" "DELETE FROM file_locks WHERE locked_by = '$(echo "$agent_name" | sed "s/'/''/g")';" 2>/dev/null || true

# Cleanup expired locks (any that hit TTL during the session)
sqlite3 "$DB_PATH" "DELETE FROM file_locks WHERE acquired_at + ttl_sec < strftime('%s','now');" 2>/dev/null || true

# Mark agent IDLE
sqlite3 "$DB_PATH" <<SQL 2>/dev/null || true
UPDATE agent_status
   SET state='IDLE',
       last_active_at=strftime('%s','now')
 WHERE agent='$(echo "$agent_name" | sed "s/'/''/g")';
SQL

exit 0
