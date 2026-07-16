#!/bin/bash
# Session reaper: finds and kills zombie claude processes + cleans their timer files.
# Layer 3 of the session lifecycle manager: on-demand or cron.
#
# Usage:
#   ~/.claude/scripts/session-reaper.sh           # kill stopped (Tl) processes
#   ~/.claude/scripts/session-reaper.sh --all     # also kill stale active sessions (>3 hours)
#   ~/.claude/scripts/session-reaper.sh --dry-run # report only, don't kill
#
# Cron (recommended):
#   */30 * * * * ~/.claude/scripts/session-reaper.sh >> ~/.claude/session-timers/reaper.log 2>&1
#
# Safe to run anytime. Only kills:
#   - T (stopped) claude processes: frozen, never resuming usefully
#   - With --all: Sl (active) claude processes older than 3 hours (likely abandoned)
# Never kills the calling process or its ancestors.
#
# Graceful retirement (item 5): sends SIGCONT then SIGTERM, giving the process
# a chance to fire SessionEnd hooks and write shutdown artifacts. Falls back to
# SIGKILL after 5 seconds if the process doesn't exit.

set -uo pipefail

# Shared session-file cleanup helper (SOT: hooks/lib.sh rm_session_files); keeps
# this script's per-session marker cleanup in lockstep with hooks/modules/40-gc.sh
# instead of maintaining its own drifting extension list.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/../hooks/lib.sh"

# ── Log rotation: keep logs bounded ──
rotate_logs() {
  local LOGDIR="$HOME/.claude/session-timers"
  local ROTATED=""

  # session-history.log: keep last 500 lines
  local HIST="$LOGDIR/session-history.log"
  if [ -f "$HIST" ]; then
    local HLINES=$(wc -l < "$HIST" 2>/dev/null || echo 0)
    if [ "$HLINES" -gt 500 ]; then
      tail -n 500 "$HIST" > "$HIST.tmp" 2>/dev/null && mv "$HIST.tmp" "$HIST" 2>/dev/null || true
      ROTATED="history $HLINES->500"
    fi
  fi

  # cleanup.log: keep last 200 lines
  local CLOG="$LOGDIR/cleanup.log"
  if [ -f "$CLOG" ]; then
    local CLINES=$(wc -l < "$CLOG" 2>/dev/null || echo 0)
    if [ "$CLINES" -gt 200 ]; then
      # Log the rotation event BEFORE rotating
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Log rotation: ${ROTATED:+history rotated, }cleanup $CLINES->200" >> "$CLOG" 2>/dev/null || true
      tail -n 200 "$CLOG" > "$CLOG.tmp" 2>/dev/null && mv "$CLOG.tmp" "$CLOG" 2>/dev/null || true
      ROTATED="${ROTATED:+$ROTATED, }cleanup $CLINES->200"
    elif [ -n "$ROTATED" ]; then
      # Log rotation of other files even if cleanup.log itself doesn't need it
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Log rotation: $ROTATED" >> "$CLOG" 2>/dev/null || true
    fi
  fi

  # reaper.log: keep last 100 lines
  local RLOG="$LOGDIR/reaper.log"
  if [ -f "$RLOG" ]; then
    local RLINES=$(wc -l < "$RLOG" 2>/dev/null || echo 0)
    if [ "$RLINES" -gt 100 ]; then
      tail -n 100 "$RLOG" > "$RLOG.tmp" 2>/dev/null && mv "$RLOG.tmp" "$RLOG" 2>/dev/null || true
      ROTATED="${ROTATED:+$ROTATED, }reaper $RLINES->100"
    fi
  fi

  if [ -n "$ROTATED" ]; then
    echo "Log rotation: $ROTATED"
  fi
}

MODE="stopped-only"
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --all) MODE="all" ;;
    --dry-run) DRY_RUN=true ;;
  esac
done

TIMER_DIR="$HOME/.claude/session-timers"
mkdir -p "$TIMER_DIR"
KILLED=0
FREED_KB=0
MY_PID=$$

# Memory pressure threshold (MB): alert if total claude RSS exceeds this
MEM_ALERT_THRESHOLD=8192
ALERT_FILE="$TIMER_DIR/memory-alert"

# Build ancestor chain (don't kill our own process tree)
ANCESTORS="$MY_PID"
WALK=$MY_PID
for _ in 1 2 3 4 5 6 7 8 9 10; do
  WALK=$(awk '{print $4}' /proc/$WALK/stat 2>/dev/null || echo "1")
  [ "$WALK" -le 1 ] && break
  ANCESTORS="$ANCESTORS $WALK"
done

# Stale threshold for --all mode (3 hours in seconds)
STALE_THRESHOLD=$((3 * 3600))

# Meta session cap: max concurrent meta sessions before oldest get killed
META_SESSION_CAP=2

echo "=== Session Reaper (mode: $MODE, dry-run: $DRY_RUN) $(date '+%Y-%m-%d %H:%M:%S') ==="
echo ""

# ── Graceful kill: SIGCONT → SIGTERM → wait → SIGKILL ──
graceful_kill() {
  local PID=$1
  local LABEL=$2

  # Resume first (stopped processes can't handle SIGTERM while frozen)
  kill -CONT "$PID" 2>/dev/null || true
  # Give it a moment to wake up
  sleep 0.2
  # SIGTERM: lets SessionEnd hook fire if Claude handles it
  kill -TERM "$PID" 2>/dev/null || true

  # Wait up to 5 seconds for graceful exit
  local WAITED=0
  while [ $WAITED -lt 5 ] && kill -0 "$PID" 2>/dev/null; do
    sleep 1
    WAITED=$((WAITED + 1))
  done

  # If still alive, force kill
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
    echo "  $LABEL: forced (SIGKILL after ${WAITED}s)"
  else
    echo "  $LABEL: graceful (exited in ${WAITED}s)"
  fi
}

# ── Find and retire claude processes ──
while IFS= read -r line; do
  [ -z "$line" ] && continue
  PID=$(echo "$line" | awk '{print $2}')
  STATE=$(echo "$line" | awk '{print $8}')
  RSS_KB=$(echo "$line" | awk '{print $6}')
  ELAPSED_S=$(ps -o etimes= -p "$PID" 2>/dev/null | tr -d ' ')
  CMD=$(echo "$line" | awk '{for(i=11;i<=NF;i++) printf "%s ", $i; print ""}')

  # Skip our ancestors
  if echo " $ANCESTORS " | grep -qw " $PID "; then
    continue
  fi

  SHOULD_KILL=false

  # Always kill stopped (T) processes
  if echo "$STATE" | grep -q "^T"; then
    SHOULD_KILL=true
    REASON="stopped (state=$STATE, ${ELAPSED_S:-?}s)"
  fi

  # In --all mode, also kill stale active sessions
  if [ "$MODE" = "all" ] && [ "$SHOULD_KILL" = false ]; then
    if [ -n "$ELAPSED_S" ] && [ "$ELAPSED_S" -gt "$STALE_THRESHOLD" ]; then
      SHOULD_KILL=true
      REASON="stale active (${ELAPSED_S}s elapsed, >${STALE_THRESHOLD}s threshold)"
    fi
  fi

  if [ "$SHOULD_KILL" = true ]; then
    RSS_MB=$((RSS_KB / 1024))
    if [ "$DRY_RUN" = true ]; then
      echo "[DRY-RUN] Would kill PID=$PID RSS=${RSS_MB}MB $REASON CMD=$CMD"
    else
      echo "Retiring PID=$PID RSS=${RSS_MB}MB $REASON CMD=$CMD"
      graceful_kill "$PID" "PID=$PID"
    fi
    KILLED=$((KILLED + 1))
    FREED_KB=$((FREED_KB + RSS_KB))
  fi
done < <(ps aux | grep '[c]laude' | grep -v grep)

# ── Meta session cap (kill oldest when count > cap) ──
META_OVER_CAP=0
META_INFO=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  PID=$(echo "$line" | awk '{print $2}')
  # Skip our ancestors
  echo " $ANCESTORS " | grep -qw " $PID " && continue
  CMDLINE=$(cat /proc/"$PID"/cmdline 2>/dev/null | tr '\0' ' ')
  if echo "$CMDLINE" | grep -qP -- '--agent\s+meta(\s|$)'; then
    ELAPSED_S=$(ps -o etimes= -p "$PID" 2>/dev/null | tr -d ' ')
    RSS_KB=$(ps -o rss= -p "$PID" 2>/dev/null | tr -d ' ')
    [ -z "$ELAPSED_S" ] && ELAPSED_S=0
    [ -z "$RSS_KB" ] && RSS_KB=0
    META_INFO="${META_INFO}${ELAPSED_S}:${RSS_KB}:${PID}"$'\n'
  fi
done < <(ps aux | grep '[c]laude' | grep -v grep)

# Sort oldest first (descending by elapsed), count non-empty lines
META_SORTED=$(echo "$META_INFO" | grep -v '^$' | sort -t: -k1 -rn)
META_COUNT=0
[ -n "$META_SORTED" ] && META_COUNT=$(echo "$META_SORTED" | wc -l)

if [ "$META_COUNT" -gt "$META_SESSION_CAP" ]; then
  KILL_COUNT=$((META_COUNT - META_SESSION_CAP))
  KILL_LIST=$(echo "$META_SORTED" | head -n "$KILL_COUNT")
  while IFS=: read -r ELAPSED_S RSS_KB KILL_PID; do
    [ -z "$KILL_PID" ] && continue
    KILL_MIN=$((ELAPSED_S / 60))
    RSS_MB=$((RSS_KB / 1024))
    if [ "$DRY_RUN" = true ]; then
      echo "[DRY-RUN] Would kill meta PID=$KILL_PID (${KILL_MIN}min old, ${RSS_MB}MB), over cap of $META_SESSION_CAP"
    else
      echo "Retiring meta PID=$KILL_PID (${KILL_MIN}min old, ${RSS_MB}MB), over cap of $META_SESSION_CAP"
      graceful_kill "$KILL_PID" "meta-cap PID=$KILL_PID"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ended: agent=meta duration=${KILL_MIN}min pid=$KILL_PID session=meta-cap exit=meta-cap" >> "$TIMER_DIR/session-history.log" 2>/dev/null || true
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Reaper: meta-cap killed PID=$KILL_PID (${KILL_MIN}min, ${RSS_MB}MB)" >> "$TIMER_DIR/cleanup.log" 2>/dev/null || true
    fi
    META_OVER_CAP=$((META_OVER_CAP + 1))
    KILLED=$((KILLED + 1))
    FREED_KB=$((FREED_KB + RSS_KB))
  done <<< "$KILL_LIST"
fi

# ── Clean orphaned timer files (no matching live process) ──
CLEANED_FILES=0
if [ -d "$TIMER_DIR" ]; then
  for pid_file in "$TIMER_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    TRACKED_PID=$(cat "$pid_file" 2>/dev/null || echo "")
    [ -z "$TRACKED_PID" ] && continue
    if ! kill -0 "$TRACKED_PID" 2>/dev/null; then
      SID=$(basename "$pid_file" .pid)
      AGENT=$(cat "$TIMER_DIR/${SID}.agent" 2>/dev/null || echo "?")
      START_EPOCH=$(cat "$TIMER_DIR/${SID}.start" 2>/dev/null || echo "")
      DURATION=""
      if [ -n "$START_EPOCH" ]; then
        DURATION="$(( ($(date +%s) - START_EPOCH) / 60 ))min"
      fi
      if [ "$DRY_RUN" = true ]; then
        echo "[DRY-RUN] Would clean timer files for session=${SID:0:8}... agent=$AGENT (PID=$TRACKED_PID dead)"
      else
        # Record session history before deleting
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ended: agent=$AGENT duration=$DURATION pid=$TRACKED_PID session=${SID:0:8} exit=reaped" >> "$TIMER_DIR/session-history.log" 2>/dev/null || true
        rm_session_files "$SID"
        echo "Cleaned timer files: session=${SID:0:8}... agent=$AGENT duration=$DURATION (PID=$TRACKED_PID dead)"
      fi
      CLEANED_FILES=$((CLEANED_FILES + 1))
    fi
  done
fi

# ── Memory pressure check ──
TOTAL_RSS_KB=$(ps aux | grep '[c]laude' | grep -v grep | awk '{sum += $6} END {print sum+0}')
TOTAL_RSS_MB=$((TOTAL_RSS_KB / 1024))
ACTIVE_COUNT=$(ps aux | grep '[c]laude' | grep -v grep | wc -l)

if [ "$TOTAL_RSS_MB" -gt "$MEM_ALERT_THRESHOLD" ]; then
  echo ""
  echo "!! MEMORY PRESSURE: ${TOTAL_RSS_MB}MB across ${ACTIVE_COUNT} claude processes (threshold: ${MEM_ALERT_THRESHOLD}MB)"
  echo "!! Run with --all to also kill stale active sessions, or manually kill specific PIDs"
  # Write alert file (meta can read this)
  echo "${TOTAL_RSS_MB}MB across ${ACTIVE_COUNT} processes at $(date '+%H:%M:%S')" > "$ALERT_FILE"
elif [ -f "$ALERT_FILE" ]; then
  # Pressure resolved: remove alert
  rm -f "$ALERT_FILE"
fi

# ── Summary ──
FREED_MB=$((FREED_KB / 1024))
echo ""
echo "=== Summary ==="
echo "Processes retired: $KILLED (${FREED_MB} MB freed)"
echo "Timer file sets cleaned: $CLEANED_FILES"
echo "Active claude processes: $ACTIVE_COUNT (${TOTAL_RSS_MB} MB total)"
if [ "$META_OVER_CAP" -gt 0 ]; then
  echo "Meta sessions: $META_COUNT active, $META_OVER_CAP over cap, killed (cap: $META_SESSION_CAP)"
else
  echo "Meta sessions: $META_COUNT active (cap: $META_SESSION_CAP)"
fi

# ── Logging ──
if [ "$DRY_RUN" = false ]; then
  LOG_FILE="$TIMER_DIR/cleanup.log"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Reaper: killed=$KILLED freed=${FREED_MB}MB cleaned=$CLEANED_FILES active=$ACTIVE_COUNT rss=${TOTAL_RSS_MB}MB mode=$MODE" >> "$LOG_FILE" 2>/dev/null
fi

# ── Log rotation (runs after all reaping) ──
rotate_logs
