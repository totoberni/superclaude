#!/bin/bash
# Pre-compact hook: save agent recovery context before context compaction.
# Fires on both auto-compact and manual /compact.
#
# Snapshots state files, latest reports, and current directives so the
# agent can recover after compaction.

set -uo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null) || SESSION_ID="unknown"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

SNAPSHOT_DIR="$HOME/.claude/agent-memory/_system/_compact-snapshots"
mkdir -p "$SNAPSHOT_DIR"

SNAPSHOT_FILE="$SNAPSHOT_DIR/compact-${TIMESTAMP}-${SESSION_ID:0:8}.md"

# Detect agent name (same process-tree walk as session-timer.sh)
AGENT_NAME=""
AGENT_FILE="$HOME/.claude/session-timers/${SESSION_ID}.agent"
if [ -f "$AGENT_FILE" ]; then
  AGENT_NAME=$(cat "$AGENT_FILE" 2>/dev/null || echo "")
else
  WALK_PID=$$
  for _ in 1 2 3 4 5 6; do
    WALK_PID=$(awk '{print $4}' /proc/$WALK_PID/stat 2>/dev/null || echo "1")
    [ "$WALK_PID" -le 1 ] && break
    CMDLINE=$(tr '\0' ' ' < /proc/$WALK_PID/cmdline 2>/dev/null || echo "")
    if echo "$CMDLINE" | grep -qP -- '--agent\s'; then
      AGENT_NAME=$(echo "$CMDLINE" | grep -oP -- '--agent\s+\K\S+' || echo "")
      break
    fi
  done
fi

# Collect recovery-relevant state
{
  echo "# Compact Snapshot"
  echo "**Time**: $(date -Iseconds)"
  echo "**Session**: $SESSION_ID"
  echo "**Agent**: ${AGENT_NAME:-unknown}"
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

  # Note any recent reports
  echo ""
  echo "## Recent Reports (last entry per orch)"
  for f in "$HOME"/.claude/comms/orch-*/reports.md; do
    if [ -f "$f" ]; then
      ORCH_NAME=$(basename "$(dirname "$f")")
      LAST_REPORT=$(grep -n "^## RPT-" "$f" 2>/dev/null | tail -1 || echo "none")
      echo "- **$ORCH_NAME**: $LAST_REPORT"
    fi
  done
} > "$SNAPSHOT_FILE" 2>/dev/null

# Keep only the last 5 snapshots to prevent unbounded growth between /lt-mem runs
ls -t "$SNAPSHOT_DIR"/compact-*.md 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true

exit 0
