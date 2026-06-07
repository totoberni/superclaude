#!/bin/bash
# Pre-compact hook: save agent recovery context before context compaction.
# Fires on both auto-compact and manual /compact.
#
# Snapshots state files, latest reports, and current directives so the
# agent can recover after compaction.

set -uo pipefail

# Source shared helpers (walk_to_agent).
. "$HOME/.claude/hooks/lib.sh" 2>/dev/null || { echo "WARN: lib.sh not found" >&2; }

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null) || SESSION_ID="unknown"
TRIGGER=$(echo "$INPUT" | jq -r '.trigger // "unknown"' 2>/dev/null) || TRIGGER="unknown"
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null) || TRANSCRIPT=""
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

SNAPSHOT_DIR="${COMPACT_SNAPSHOT_DIR:-$HOME/.claude/agent-memory/_system/_compact-snapshots}"
mkdir -p "$SNAPSHOT_DIR"

SNAPSHOT_FILE="$SNAPSHOT_DIR/compact-${TIMESTAMP}-${SESSION_ID:0:8}.md"

# PID-liveness check: skip orchs whose tracked sessions are all dead.
# Mirrors 40-gc.sh:24 (`kill -0`). If no .pid file exists, default to alive
# (orch may not track a PID — don't lose its snapshot).
TIMER_DIR="${TIMER_DIR:-$HOME/.claude/session-timers}"
is_orch_alive() {
  local orch="$1"
  local found_any=0
  for af in "$TIMER_DIR"/*.agent; do
    [ -f "$af" ] || continue
    grep -q "^${orch}$" "$af" || continue
    found_any=1
    local sid
    sid=$(basename "$af" .agent)
    local pid_file="$TIMER_DIR/${sid}.pid"
    [ -f "$pid_file" ] || return 0  # no PID file = unknown, default to alive
    local pid
    pid=$(cat "$pid_file" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
  done
  # No .agent file references this orch at all → assume alive (orch may
  # not register session timers; preserve the snapshot).
  [ "$found_any" -eq 0 ] && return 0
  return 1
}

# Detect agent name (cached file or proc-tree walk via lib.sh helper)
AGENT_NAME=""
AGENT_FILE="$HOME/.claude/session-timers/${SESSION_ID}.agent"
if [ -f "$AGENT_FILE" ]; then
  AGENT_NAME=$(cat "$AGENT_FILE" 2>/dev/null || echo "")
else
  AGENT_NAME=$(walk_to_agent "$$" 6 2>/dev/null || echo "")
fi

# Collect recovery-relevant state
{
  echo "# Compact Snapshot"
  echo "**Time**: $(date -Iseconds)"
  echo "**Session**: $SESSION_ID"
  echo "**Agent**: ${AGENT_NAME:-unknown}"
  echo "**Trigger**: ${TRIGGER}"
  echo ""

  # Snapshot all per-orch state files
  echo "## Active State Files"
  for f in "$HOME"/.claude/plans/*/state*.md; do
    if [ -f "$f" ]; then
      echo ""
      echo "### $(basename "$f")"
      echo '```'
      head -20 "$f"
      echo '```'
    fi
  done

  # Latest directive for the compacting agent (most critical recovery context)
  if [ -n "$AGENT_NAME" ] && [ -d "$HOME/.claude/comms/$AGENT_NAME" ]; then
    echo ""
    echo "## Current Directive ($AGENT_NAME)"
    DIRECTIVE_FILE="$HOME/.claude/comms/$AGENT_NAME/directives.md"
    if [ -f "$DIRECTIVE_FILE" ]; then
      # Grab the last DIR-NNN entry
      LAST_DIR_LINE=$(grep -n "^## DIR-" "$DIRECTIVE_FILE" 2>/dev/null | tail -1 | cut -d: -f1 || echo "")
      if [ -n "$LAST_DIR_LINE" ]; then
        echo '```'
        tail -n +"$LAST_DIR_LINE" "$DIRECTIVE_FILE" | head -30
        echo '```'
      else
        echo "(no directives found)"
      fi
    fi
  fi

  # Note any recent reports — skip dead orchs (PID-liveness gate)
  echo ""
  echo "## Recent Reports (last entry per orch)"
  for f in "$HOME"/.claude/comms/orch-*/reports.md; do
    if [ -f "$f" ]; then
      ORCH_NAME=$(basename "$(dirname "$f")")
      if ! is_orch_alive "$ORCH_NAME"; then
        echo "- **$ORCH_NAME**: (skipped: orch session(s) dead)"
        continue
      fi
      LAST_REPORT=$(grep -n "^## RPT-" "$f" 2>/dev/null | tail -1 || echo "none")
      echo "- **$ORCH_NAME**: $LAST_REPORT"
    fi
  done
  # T9.10: Recent activity from transcript (fail-safe — omits section if unavailable)
  if [ -n "$TRANSCRIPT" ] && [ -r "$TRANSCRIPT" ]; then
    echo ""
    echo "## Recent Activity (from transcript)"
    # (a) Last user message text
    LAST_USER_MSG=$(tail -n 400 "$TRANSCRIPT" 2>/dev/null | \
      jq -r 'select(type == "object") | select(.type == "user") |
             .message.content |
             if type == "string" then .
             elif type == "array" then (map(select(.type == "text") | .text) | join(" "))
             else "" end' 2>/dev/null | \
      grep -v '^$' | tail -1) || LAST_USER_MSG=""
    if [ -n "$LAST_USER_MSG" ]; then
      echo "**Last user message**: $(echo "$LAST_USER_MSG" | head -c 400)"
    fi
    # (b) Last ~8 distinct file paths from Edit/Write/Read tool_use entries
    RECENT_FILES=$(tail -n 400 "$TRANSCRIPT" 2>/dev/null | \
      jq -r 'select(type == "object") | select(.type == "assistant") |
             .message.content[]? |
             select(.type == "tool_use") |
             select(.name == "Edit" or .name == "Write" or .name == "Read") |
             (.input.file_path // .input.notebook_path // "") |
             select(length > 0)' 2>/dev/null | \
      awk '!seen[$0]++' | tail -8 | tac) || RECENT_FILES=""
    if [ -n "$RECENT_FILES" ]; then
      echo "**Files recently touched** (most-recent first):"
      echo "$RECENT_FILES" | while IFS= read -r fp; do
        echo "  - $fp"
      done
    fi
  fi
} > "$SNAPSHOT_FILE" 2>/dev/null

# Keep only the last 5 snapshots to prevent unbounded growth between /lt-mem runs
ls -t "$SNAPSHOT_DIR"/compact-*.md 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true

exit 0
