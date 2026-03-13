#!/bin/bash
# Session status: structured overview of all claude processes and timer files.
# Called by the /session-reaper skill (status subcommand) and readable by agents.

set -uo pipefail

TIMER_DIR="$HOME/.claude/session-timers"
NOW=$(date +%s)

echo "=== Claude Processes ==="
echo "PID|STATE|RSS_MB|AGENT|ELAPSED|CMD"

while IFS= read -r line; do
  [ -z "$line" ] && continue
  # Skip bash subprocess shells (shell-snapshots, session-status.sh itself)
  echo "$line" | grep -q 'shell-snapshots\|session-status\|session-reaper' && continue
  PID=$(echo "$line" | awk '{print $2}')
  STATE=$(echo "$line" | awk '{print $8}')
  RSS_KB=$(echo "$line" | awk '{print $6}')
  RSS_MB=$((RSS_KB / 1024))
  ELAPSED_S=$(ps -o etimes= -p "$PID" 2>/dev/null | tr -d ' ')
  if [ -n "$ELAPSED_S" ]; then
    ELAPSED_MIN=$((ELAPSED_S / 60))
    ELAPSED="${ELAPSED_MIN}min"
  else
    ELAPSED="?"
  fi
  # Extract agent name from command
  CMD=$(echo "$line" | awk '{for(i=11;i<=NF;i++) printf "%s ", $i; print ""}' | sed 's/ *$//')
  AGENT=$(echo "$CMD" | grep -oP -- '--agent\s+\K\S+' || echo "-")
  echo "$PID|$STATE|${RSS_MB}|$AGENT|$ELAPSED|$CMD"
done < <(ps aux | { grep '[c]laude' || true; } | { grep -v grep || true; })

echo ""
echo "=== Memory ==="
TOTAL_KB=$(ps aux | grep '[c]laude' | grep -v grep | awk '{sum+=$6} END {print sum+0}')
TOTAL_MB=$((TOTAL_KB / 1024))
# Zombie detection: use { cmd || true; } to prevent pipefail exit on empty grep
ALL_PROCS=$(ps aux | { grep '[c]laude' || true; } | { grep -v grep || true; })
ZOMBIE_KB=$(echo "$ALL_PROCS" | { grep ' Tl ' || true; } | awk '{sum+=$6} END {print sum+0}')
ZOMBIE_MB=$((ZOMBIE_KB / 1024))
ZOMBIE_COUNT=$(echo "$ALL_PROCS" | { grep -c ' Tl ' || true; })
ACTIVE_COUNT=$(echo "$ALL_PROCS" | { grep -c '.' || true; })
echo "total_mb=$TOTAL_MB"
echo "zombie_mb=$ZOMBIE_MB"
echo "zombie_count=$ZOMBIE_COUNT"
echo "active_count=$ACTIVE_COUNT"
echo "threshold_mb=8192"
if [ -f "$TIMER_DIR/memory-alert" ]; then
  echo "alert=$(cat "$TIMER_DIR/memory-alert")"
else
  echo "alert=none"
fi

echo ""
echo "=== Timer Files ==="
echo "SESSION|AGENT|STARTED|PID|PID_ALIVE"
for start_file in "$TIMER_DIR"/*.start; do
  [ -f "$start_file" ] || continue
  SID=$(basename "$start_file" .start)
  AGENT=$(cat "$TIMER_DIR/${SID}.agent" 2>/dev/null || echo "?")
  START_EPOCH=$(cat "$start_file" 2>/dev/null || echo "0")
  ELAPSED_MIN=$(( (NOW - START_EPOCH) / 60 ))
  TRACKED_PID=$(cat "$TIMER_DIR/${SID}.pid" 2>/dev/null || echo "-")
  if [ "$TRACKED_PID" = "-" ]; then
    ALIVE="no-pid"
  elif kill -0 "$TRACKED_PID" 2>/dev/null; then
    PROC_STATE=$(awk '{print $3}' /proc/$TRACKED_PID/stat 2>/dev/null || echo "?")
    ALIVE="yes($PROC_STATE)"
  else
    ALIVE="dead"
  fi
  echo "${SID:0:8}|$AGENT|${ELAPSED_MIN}min|$TRACKED_PID|$ALIVE"
done

echo ""
echo "=== Cron ==="
if crontab -l 2>/dev/null | grep -q "session-reaper"; then
  echo "installed=true"
  LAST_REAPER=$(tail -1 "$TIMER_DIR/reaper.log" 2>/dev/null || echo "never")
  echo "last_run=$LAST_REAPER"
else
  echo "installed=false"
fi
