#!/bin/bash
# SessionEnd hook: cleans up timer files when a session exits normally.
# Fires on /exit, Ctrl+C, /clear, logout, and natural termination.
# Cannot block — exit codes are ignored by Claude Code for SessionEnd.
#
# This is Layer 1 of the session lifecycle manager.
# Layer 2: session-timer.sh PID-liveness GC (catches abnormal exits)
# Layer 3: session-reaper.sh (manual/cron zombie cleanup)

set -uo pipefail

# Source shared helpers (rm_session_files).
. "$HOME/.claude/hooks/lib.sh" 2>/dev/null || { echo "WARN: lib.sh not found" >&2; }

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

# Delete all timer files for this session (lib.sh::rm_session_files handles
# chmod + rm). For this session-id it removes every per-session sidecar:
#   .start  .agent  .pid  .override  .calls  .tdd  .context-warned
#   .baseline-stashed  .commit-gate-warned  .bootstrap-warned
# (the canonical extension list lives in rm_session_files; mirrored here for
#  readers auditing what a normal SessionEnd cleans without cross-referencing lib.sh).
rm_session_files "$SESSION_ID"

# Record session history (structured log for analytics)
HISTORY_FILE="$TIMER_DIR/session-history.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ended: agent=${AGENT_NAME:-bare} duration=${DURATION:-?} session=${SESSION_ID:0:8} exit=$REASON" >> "$HISTORY_FILE" 2>/dev/null || true
# Keep history manageable
tail -500 "$HISTORY_FILE" > "$HISTORY_FILE.tmp" 2>/dev/null && mv "$HISTORY_FILE.tmp" "$HISTORY_FILE" 2>/dev/null || true

# Log the cleanup
LOG_FILE="$TIMER_DIR/cleanup.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] SessionEnd: ${AGENT_NAME:-bare} session=${SESSION_ID:0:8} reason=$REASON duration=${DURATION:-?}" >> "$LOG_FILE" 2>/dev/null
# Keep well under 200-line budget (R4.5 threshold). Auto-rotate to 100 when exceeding 200.
[ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 200 ] && {
  tail -100 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null || true
}

# Auto-archive stale orchs (audit O12): scan registry for Decommissioned rows
# whose comms/<orch>/ still exists, mv them to comms/_archive/. Excludes the
# current session's own orch as a safety guard.
ARCHIVE_SCRIPT="$HOME/.claude/scripts/auto-archive-stale-orchs.sh"
if [ -x "$ARCHIVE_SCRIPT" ]; then
  ARCHIVE_OUT=$(bash "$ARCHIVE_SCRIPT" --exclude "$AGENT_NAME" 2>&1 | head -20)
  if [ -n "$ARCHIVE_OUT" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] auto-archive: $(echo "$ARCHIVE_OUT" | tail -1)" >> "$LOG_FILE" 2>/dev/null || true
  fi
fi

# history.jsonl rotation (v3 fix #6): the append-only session-history ledger grows
# unbounded (~per session). When it crosses 10MB, gzip a timestamped snapshot into
# _backups/history/ and truncate the live file, retaining the 5 most recent archives.
# Fully fail-safe — any error here must not affect session cleanup (|| true everywhere).
# Target dir is gitignored (_backups/), so archives never enter the public repo.
HISTORY_JSONL="$HOME/.claude/history.jsonl"
HISTORY_ROTATE_BYTES=10485760   # 10 MiB
HISTORY_ARCHIVE_KEEP=5
if [ -f "$HISTORY_JSONL" ]; then
  HJ_SIZE=$(stat -c%s "$HISTORY_JSONL" 2>/dev/null || echo 0)
  if [ "$HJ_SIZE" -gt "$HISTORY_ROTATE_BYTES" ] 2>/dev/null; then
    HJ_ARCHIVE_DIR="$HOME/.claude/_backups/history"
    mkdir -p "$HJ_ARCHIVE_DIR" 2>/dev/null || true
    HJ_TS=$(date '+%Y%m%d-%H%M%S')
    if gzip -c "$HISTORY_JSONL" > "$HJ_ARCHIVE_DIR/history-$HJ_TS.jsonl.gz" 2>/dev/null; then
      : > "$HISTORY_JSONL"   # truncate in place (preserves inode/perms; CC keeps appending)
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] history rotate: ${HJ_SIZE}B -> history-$HJ_TS.jsonl.gz, truncated" >> "$LOG_FILE" 2>/dev/null || true
      # Retain only the newest $HISTORY_ARCHIVE_KEEP archives (delete older).
      ls -1t "$HJ_ARCHIVE_DIR"/history-*.jsonl.gz 2>/dev/null | tail -n +$((HISTORY_ARCHIVE_KEEP + 1)) | while IFS= read -r old; do
        rm -f "$old" 2>/dev/null || true
      done
    fi
  fi
fi

exit 0
