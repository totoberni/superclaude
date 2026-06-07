#!/usr/bin/env bash
# hcom-pre-tool-use.sh
# Event: PreToolUse (every tool call)
# Purpose: inject any pending HCOM messages addressed to the current agent
#          into the agent's next prompt frame, mid-turn.
# Phase A: opt-in (only fires if broker DB exists and sqlite3 available).
# Non-blocking: silent on failure.
#
# IMPORTANT (2026-05-09): time-filter on session start to prevent auto-injection
# of historical/backfilled messages. Only injects messages with ts > session_start.
#
# FAIL-SAFE: exit 0 ALWAYS. NEVER `set -e` — this is a PreToolUse hook on every
# tool call; a single transient non-zero (sqlite/jq hiccup) must not abort the
# CLI mid-turn. Every fallible command below is guarded with `|| exit 0` / `|| true`.
set -uo pipefail

DB_PATH="$HOME/.claude/comms/.broker.db"
TIMER_DIR="$HOME/.claude/session-timers"

# Phase A opt-in: silent no-op if HCOM not initialized
[ -f "$DB_PATH" ] || exit 0
command -v sqlite3 >/dev/null 2>&1 || exit 0

# Read hook input from stdin
input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // ""' 2>/dev/null)
[ -z "$session_id" ] && exit 0

# Resolve session start time (filter older messages out)
start_file="$TIMER_DIR/${session_id}.start"
if [ -f "$start_file" ]; then
  session_start=$(cat "$start_file" 2>/dev/null)
else
  # No start file — fall back to current time minus 1 hour as conservative window
  # (worst case: misses messages older than 1h, which is acceptable for new sessions)
  session_start=$(($(date +%s) - 3600))
fi
[ -z "$session_start" ] && exit 0

# Resolve current agent name (from timer files or env)
agent_name="${CLAUDE_AGENT_NAME:-}"
if [ -z "$agent_name" ]; then
  agent_file="$TIMER_DIR/${session_id}.agent"
  [ -f "$agent_file" ] && agent_name=$(cat "$agent_file" 2>/dev/null)
fi
[ -z "$agent_name" ] && exit 0

# Fetch pending messages via direct sqlite3 (time-filtered, body-truncated, capped)
# - Filter: addressed to me (agent_name, @agent_name, *), unread, AND ts > session_start
# - Cap: 5 messages per fire
# - Truncate: body to 300 chars
msgs=$(sqlite3 -separator $'\t' "$DB_PATH" "
  SELECT id, kind, COALESCE(seq, 0), from_agent, ts, substr(body, 1, 300)
  FROM messages
  WHERE (to_agent = '$agent_name' OR to_agent = '@$agent_name' OR to_agent = '*')
    AND read_at IS NULL
    AND ts > $session_start
  ORDER BY ts ASC
  LIMIT 5;
" 2>/dev/null) || exit 0

[ -z "$msgs" ] && exit 0

# Build context message + collect IDs for marking read
ctx="HCOM messages addressed to you (auto-injected mid-turn):\n\n"
ids=""
first=1
while IFS=$'\t' read -r id kind seq from_agent ts body; do
  [ -z "$id" ] && continue
  if [ -z "$ids" ]; then
    ids="$id"
  else
    ids="$ids,$id"
  fi
  if [ $first -eq 1 ]; then
    first=0
  else
    ctx="${ctx}\n\n---\n\n"
  fi
  if [ "$seq" != "0" ] && [ -n "$seq" ]; then
    ctx="${ctx}**${kind}-${seq}** from ${from_agent} (ts: ${ts}):\n${body}"
  else
    ctx="${ctx}**${kind}** from ${from_agent} (ts: ${ts}):\n${body}"
  fi
done <<< "$msgs"

# Mark fetched messages as read
if [ -n "$ids" ]; then
  sqlite3 "$DB_PATH" "UPDATE messages SET read_at = strftime('%s','now') WHERE id IN ($ids);" 2>/dev/null || true
fi

# Emit additionalContext
if [ -n "$ctx" ]; then
  jq -nc --arg ctx "$(printf '%b' "$ctx")" '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $ctx}}'
fi

exit 0
