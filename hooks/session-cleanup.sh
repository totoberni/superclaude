#!/bin/bash
# SessionEnd hook: cleans up timer files when a session exits normally.
# Fires on /exit, Ctrl+C, /clear, logout, and natural termination.
# Cannot block — exit codes are ignored by Claude Code for SessionEnd.
#
# This is Layer 1 of the session lifecycle manager.
# Layer 2: session-timer.sh PID-liveness GC (catches abnormal exits)
# Layer 3: session-reaper.sh (manual/cron zombie cleanup)

set -uo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"')
REASON=$(echo "$INPUT" | jq -r '.reason // "unknown"')

TIMER_DIR="$HOME/.claude/session-timers"

[ "$SESSION_ID" = "unknown" ] && exit 0
[ ! -d "$TIMER_DIR" ] && exit 0

# Read session metadata before deleting files (for logging + history)
AGENT_NAME=""
START_EPOCH=""
DURATION=""
if [ -f "$TIMER_DIR/${SESSION_ID}.agent" ]; then
  AGENT_NAME=$(cat "$TIMER_DIR/${SESSION_ID}.agent" 2>/dev/null || echo "")
fi
if [ -f "$TIMER_DIR/${SESSION_ID}.start" ]; then
  START_EPOCH=$(cat "$TIMER_DIR/${SESSION_ID}.start" 2>/dev/null || echo "")
  if [[ "$START_EPOCH" =~ ^[0-9]+$ ]]; then
    DURATION="$(( ($(date +%s) - START_EPOCH) / 60 ))min"
  fi
fi

# Delete all timer files for this session (chmod first — start file may be read-only)
chmod 644 "$TIMER_DIR/${SESSION_ID}.start" 2>/dev/null || true
rm -f "$TIMER_DIR/${SESSION_ID}".{start,agent,pid,override,calls,tdd,context-warned}

# Record session history (structured log for analytics)
HISTORY_FILE="$TIMER_DIR/session-history.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ended: agent=${AGENT_NAME:-bare} duration=${DURATION:-?} session=${SESSION_ID:0:8} exit=$REASON" >> "$HISTORY_FILE" 2>/dev/null || true
# Keep history manageable
tail -500 "$HISTORY_FILE" > "$HISTORY_FILE.tmp" 2>/dev/null && mv "$HISTORY_FILE.tmp" "$HISTORY_FILE" 2>/dev/null || true

# Log the cleanup
LOG_FILE="$TIMER_DIR/cleanup.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] SessionEnd: ${AGENT_NAME:-bare} session=${SESSION_ID:0:8} reason=$REASON duration=${DURATION:-?}" >> "$LOG_FILE" 2>/dev/null
tail -200 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null || true

exit 0
