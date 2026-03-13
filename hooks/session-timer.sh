#!/bin/bash
# Session timer hook: enforces time limits on all named agent sessions.
# Registered for SessionStart and PreToolUse in settings.json.
#
# Timeline:
#   0-35 min  — normal operation
#   35-40 min — WARNING (non-blocking, tells agent to wrap up)
#   40-48 min — GRACE PERIOD (only shutdown ops: state/report/memory writes, git commits)
#   48+ min   — HARD BLOCK (all tool calls rejected)
#
# Session lifecycle (Layer 2 — PID-liveness GC):
#   - Tracks parent claude PID in <session_id>.pid
#   - On each invocation, checks all tracked PIDs:
#     - Dead PID → clean up that session's timer files
#     - Stopped PID (T state) → kill process + clean up timer files
#   - Layer 1 (session-cleanup.sh / SessionEnd) handles normal exits
#   - Layer 3 (session-reaper.sh) handles manual batch cleanup
#
# Exempt: meta agent, bare claude (no --agent).
# Override: touch ~/.claude/session-timers/<session_id>.override
#
# Exit codes: 0 = allow, 2 = block

set -euo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null) || SESSION_ID="unknown"
# Sanitize session ID — prevent path traversal and shell injection
SESSION_ID=$(printf '%s' "$SESSION_ID" | tr -cd 'a-zA-Z0-9_-')
[ -z "$SESSION_ID" ] && SESSION_ID="unknown"

TIMER_DIR="$HOME/.claude/session-timers"
mkdir -p "$TIMER_DIR"

START_FILE="$TIMER_DIR/${SESSION_ID}.start"
OVERRIDE_FILE="$TIMER_DIR/${SESSION_ID}.override"
AGENT_FILE="$TIMER_DIR/${SESSION_ID}.agent"
PID_FILE="$TIMER_DIR/${SESSION_ID}.pid"

# ── Garbage collection (2 phases) ──

# Phase 1: remove files from ended sessions (>2 hours old) — failsafe
find "$TIMER_DIR" -maxdepth 1 -type f -name "*.start" -mmin +120 -exec basename {} .start \; 2>/dev/null | while read -r STALE_SID; do
  chmod 644 "$TIMER_DIR/${STALE_SID}.start" 2>/dev/null || true
  rm -f "$TIMER_DIR/${STALE_SID}".{start,agent,pid,override,calls}
done

# Phase 2: PID-liveness check — detect dead/stopped sessions
for PF in "$TIMER_DIR"/*.pid; do
  [ -f "$PF" ] || continue
  TRACKED_SID=$(basename "$PF" .pid)
  # Don't reap our own session
  [ "$TRACKED_SID" = "$SESSION_ID" ] && continue
  TRACKED_PID=$(cat "$PF" 2>/dev/null || echo "")
  [ -z "$TRACKED_PID" ] && continue

  if ! kill -0 "$TRACKED_PID" 2>/dev/null; then
    # PID is dead — clean up (mode #3: crash/kill/OOM)
    chmod 644 "$TIMER_DIR/${TRACKED_SID}.start" 2>/dev/null || true
    rm -f "$TIMER_DIR/${TRACKED_SID}".{start,agent,pid,override,calls}
  else
    # PID alive — check if stopped (T state = frozen, never resuming usefully)
    PROC_STATE=$(awk '{print $3}' /proc/$TRACKED_PID/stat 2>/dev/null || echo "")
    if echo "$PROC_STATE" | grep -q "^T"; then
      TRACKED_AGENT=$(cat "$TIMER_DIR/${TRACKED_SID}.agent" 2>/dev/null || echo "?")
      TRACKED_START=$(cat "$TIMER_DIR/${TRACKED_SID}.start" 2>/dev/null || echo "")
      TRACKED_DUR=""
      [[ "$TRACKED_START" =~ ^[0-9]+$ ]] && TRACKED_DUR="$(( ($(date +%s) - TRACKED_START) / 60 ))min"
      # Graceful retirement: SIGCONT → SIGTERM (let SessionEnd fire if possible)
      kill -CONT "$TRACKED_PID" 2>/dev/null || true
      kill -TERM "$TRACKED_PID" 2>/dev/null || true
      # Record history before cleaning files
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ended: agent=$TRACKED_AGENT duration=${TRACKED_DUR:-?} pid=$TRACKED_PID session=${TRACKED_SID:0:8} exit=gc-stopped" >> "$TIMER_DIR/session-history.log" 2>/dev/null || true
      chmod 644 "$TIMER_DIR/${TRACKED_SID}.start" 2>/dev/null || true
      rm -f "$TIMER_DIR/${TRACKED_SID}".{start,agent,pid,override,calls}
      echo "[$(date '+%H:%M:%S')] GC: retired stopped $TRACKED_AGENT (PID=$TRACKED_PID, ${TRACKED_DUR:-?})" >> "$TIMER_DIR/cleanup.log" 2>/dev/null || true
    fi
  fi
done

# Phase 3: clean orphan .agent/.pid files (sessions without .start, >2h old)
# Edge case: sessions that crashed during agent detection before .start creation.
for AF in "$TIMER_DIR"/*.agent; do
  [ -f "$AF" ] || continue
  ORPHAN_SID=$(basename "$AF" .agent)
  # Don't touch our own session
  [ "$ORPHAN_SID" = "$SESSION_ID" ] && continue
  # Skip if .start exists (normal session — Phase 1/2 handle these)
  [ -f "$TIMER_DIR/${ORPHAN_SID}.start" ] && continue
  # Skip if .agent is <2 hours old (session may still be setting up)
  if [ "$(find "$AF" -mmin +120 2>/dev/null)" ]; then
    rm -f "$TIMER_DIR/${ORPHAN_SID}".{agent,pid,override,calls}
  fi
done

# ── Detect agent type + parent PID (cached after first detection) ──
AGENT_NAME=""
CLAUDE_PID=""
if [ -f "$AGENT_FILE" ]; then
  AGENT_NAME=$(cat "$AGENT_FILE" 2>/dev/null || echo "")
else
  # Walk up process tree to find `claude --agent <name>`
  WALK_PID=$$
  for _ in 1 2 3 4 5 6 7 8; do
    WALK_PID=$(awk '{print $4}' /proc/$WALK_PID/stat 2>/dev/null || echo "1")
    [ "$WALK_PID" -le 1 ] && break
    CMDLINE=$(tr '\0' ' ' < /proc/$WALK_PID/cmdline 2>/dev/null || echo "")
    if echo "$CMDLINE" | grep -q "claude"; then
      CLAUDE_PID="$WALK_PID"
      if echo "$CMDLINE" | grep -qP -- '--agent\s'; then
        AGENT_NAME=$(echo "$CMDLINE" | grep -oP -- '--agent\s+\K\S+' || echo "")
        break
      fi
    fi
  done
  # Cache agent name (even empty string — so we don't re-walk on every tool call)
  echo "$AGENT_NAME" > "$AGENT_FILE"
  # Cache claude PID for lifecycle tracking
  if [ -n "$CLAUDE_PID" ]; then
    echo "$CLAUDE_PID" > "$PID_FILE"
  fi
fi

# ── Nudge detection (all agents) ──
# Runs for ANY named agent, before the orch-only gate.
# One-shot: read first line, extract digits only, delete file, emit JSON additionalContext.
NUDGE_FIRED=false
if [ -n "$AGENT_NAME" ]; then
  NUDGE_FILE="$HOME/.claude/nudge/$AGENT_NAME"
  NUDGE_CHARS=$(head -1 "$NUDGE_FILE" 2>/dev/null | tr -cd '0-9') || true
  if [ -n "$NUDGE_CHARS" ]; then
    rm -f "$NUDGE_FILE"
    NUDGE_FIRED=true
    printf '{"additionalContext":"NUDGE from the user: Produce a status report NOW. Format: [NUDGE] %s | {elapsed} | {emoji}\\n{description, max %s chars: current task, status, blocker if any}\\nThen resume your work."}\n' "$AGENT_NAME" "$NUDGE_CHARS"
  elif [ -e "$NUDGE_FILE" ]; then
    # Empty, invalid, or non-file nudge entry — clean it up
    rm -rf "$NUDGE_FILE" 2>/dev/null || true
  fi
fi

# ── Tool call efficiency counter ──
if [ -n "$AGENT_NAME" ] && [ "$AGENT_NAME" != "meta" ]; then
  COUNTER_FILE="$TIMER_DIR/${SESSION_ID}.calls"
  CALL_COUNT=0
  [ -f "$COUNTER_FILE" ] && CALL_COUNT=$(cat "$COUNTER_FILE" 2>/dev/null | tr -cd '0-9')
  [ -z "$CALL_COUNT" ] && CALL_COUNT=0
  CALL_COUNT=$((CALL_COUNT + 1))
  echo "$CALL_COUNT" > "$COUNTER_FILE"

  if [ "$NUDGE_FIRED" = false ] && [ $((CALL_COUNT % 25)) -eq 0 ] && [ "$CALL_COUNT" -gt 0 ]; then
    printf '{"additionalContext":"Efficiency check (%d tool calls this session). Consider: (1) delegate parallelizable work to workers, (2) use offset/limit for large files, (3) batch related operations, (4) let worker failures stand -- do not redo their work yourself."}\n' "$CALL_COUNT"
  fi
fi

# ── First call: record start time for ALL named agents (including meta) ──
if [ -n "$AGENT_NAME" ] && [ ! -f "$START_FILE" ]; then
  date +%s > "$START_FILE"
  chmod 444 "$START_FILE" 2>/dev/null || true
fi

# ── Exempt: meta and bare claude only ──
# All other named agents (orch-*, scaffolder, workers) get timer enforcement.
if [ -z "$AGENT_NAME" ] || [ "$AGENT_NAME" = "meta" ]; then
  exit 0
fi

# ── From here: all named agents (orch-*, scaffolder, etc.) ──

# Check for the user's override
if [ -f "$OVERRIDE_FILE" ]; then
  exit 0
fi

# ── Check elapsed time ──
START_TIME=$(cat "$START_FILE" 2>/dev/null || echo "")
# Validate: must be a positive integer (epoch seconds)
if ! [[ "$START_TIME" =~ ^[0-9]+$ ]]; then
  echo "WARNING: corrupted timer ($START_FILE). Resetting session clock." >&2
  chmod 644 "$START_FILE" 2>/dev/null || true
  date +%s > "$START_FILE"
  chmod 444 "$START_FILE" 2>/dev/null || true
  exit 0
fi
CURRENT_TIME=$(date +%s)
ELAPSED=$((CURRENT_TIME - START_TIME))
WARN_SECONDS=$((35 * 60))   # 35 min
SOFT_SECONDS=$((40 * 60))   # 40 min — grace period starts
HARD_SECONDS=$((48 * 60))   # 48 min — hard block

MINUTES=$((ELAPSED / 60))

# ── Hard block (48+ min) ──
if [ $ELAPSED -gt $HARD_SECONDS ]; then
  echo "HARD SESSION LIMIT (${MINUTES} min). ALL tool calls blocked." >&2
  echo "Session is over. Start a new session or override: touch $OVERRIDE_FILE" >&2
  exit 2
fi

# ── Grace period (40-48 min) — only shutdown ops allowed ──
if [ $ELAPSED -gt $SOFT_SECONDS ]; then
  REMAINING=$(( (HARD_SECONDS - ELAPSED) / 60 ))
  TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
  ALLOWED=false

  case "$TOOL_NAME" in
    Write|Edit)
      # Allow writes ONLY to ~/.claude/ (state, reports, memory, comms)
      FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""')
      if echo "$FILE_PATH" | grep -q "^$HOME/.claude/"; then
        ALLOWED=true
      fi
      ;;
    Bash)
      # Allow git operations for wrapping up uncommitted work
      COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
      if echo "$COMMAND" | grep -qP '(^|\s)git\s+(-C\s+\S+\s+)?(add|commit|status|diff|log)\b'; then
        ALLOWED=true
      fi
      ;;
    Read|Glob|Grep)
      # Allow read-only ops (needed to check state before writing)
      ALLOWED=true
      ;;
  esac

  if [ "$ALLOWED" = true ]; then
    echo "GRACE PERIOD (${MINUTES} min, ~${REMAINING} min left). Shutdown ops only." >&2
    echo "  1. Commit work  2. Write RPT  3. /mistake + /good-idea  4. MEMORY.md" >&2
    exit 0
  else
    echo "GRACE PERIOD (${MINUTES} min). Tool call BLOCKED — not a shutdown operation." >&2
    echo "Allowed: Write/Edit to ~/.claude/*, git add/commit/status, Read/Glob/Grep." >&2
    echo "Override: touch $OVERRIDE_FILE" >&2
    exit 2
  fi
fi

# ── Warning (35-40 min) ──
if [ $ELAPSED -gt $WARN_SECONDS ]; then
  REMAINING=$(( (SOFT_SECONDS - ELAPSED) / 60 ))
  echo "TIME WARNING (${MINUTES} min, ~${REMAINING} min until grace period)." >&2
  echo "Wrap up current task. After 40 min: shutdown ops only. After 48 min: blocked." >&2
fi

exit 0
