# Module: Garbage collection — 3 phases (stale/dead/orphan cleanup)
# Reads: TIMER_DIR, SESSION_ID
#
# Phase 1: remove files from ended sessions (>2 hours old) — failsafe
# Phase 2: PID-liveness check — detect dead/stopped sessions
# Phase 3: clean orphan .agent/.pid files (sessions without .start, >2h old)

mod_gc() {
  # ── Phase 1: stale cleanup ──
  find "$TIMER_DIR" -maxdepth 1 -type f -name "*.start" -mmin +120 -exec basename {} .start \; 2>/dev/null | while read -r STALE_SID; do
    rm_session_files "$STALE_SID"
  done

  # ── Phase 2: PID-liveness check ──
  for PF in "$TIMER_DIR"/*.pid; do
    [ -f "$PF" ] || continue
    TRACKED_SID=$(basename "$PF" .pid)
    # Don't reap our own session
    [ "$TRACKED_SID" = "$SESSION_ID" ] && continue
    TRACKED_PID=$(cat "$PF" 2>/dev/null || echo "")
    [ -z "$TRACKED_PID" ] && continue

    if ! kill -0 "$TRACKED_PID" 2>/dev/null; then
      # PID is dead — clean up (mode #3: crash/kill/OOM)
      rm_session_files "$TRACKED_SID"
    else
      # PID alive — check if stopped (T state = frozen, never resuming usefully)
      PROC_STATE=$(awk '{print $3}' /proc/$TRACKED_PID/stat 2>/dev/null || echo "")
      if echo "$PROC_STATE" | grep -q "^T"; then
        TRACKED_AGENT=$(cat "$TIMER_DIR/${TRACKED_SID}.agent" 2>/dev/null || echo "?")
        TRACKED_START=$(cat "$TIMER_DIR/${TRACKED_SID}.start" 2>/dev/null || echo "")
        TRACKED_DUR=""
        [[ "$TRACKED_START" =~ ^[0-9]+$ ]] && TRACKED_DUR="$(( ($(date +%s) - TRACKED_START) / 60 ))min"
        # Graceful retirement: SIGCONT -> SIGTERM (let SessionEnd fire if possible)
        kill -CONT "$TRACKED_PID" 2>/dev/null || true
        kill -TERM "$TRACKED_PID" 2>/dev/null || true
        # Record history before cleaning files
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ended: agent=$TRACKED_AGENT duration=${TRACKED_DUR:-?} pid=$TRACKED_PID session=${TRACKED_SID:0:8} exit=gc-stopped" >> "$TIMER_DIR/session-history.log" 2>/dev/null || true
        rm_session_files "$TRACKED_SID"
        echo "[$(date '+%H:%M:%S')] GC: retired stopped $TRACKED_AGENT (PID=$TRACKED_PID, ${TRACKED_DUR:-?})" >> "$TIMER_DIR/cleanup.log" 2>/dev/null || true
      fi
    fi
  done

  # ── Phase 3: clean orphan .agent/.pid files (sessions without .start, >2h old) ──
  for AF in "$TIMER_DIR"/*.agent; do
    [ -f "$AF" ] || continue
    ORPHAN_SID=$(basename "$AF" .agent)
    # Don't touch our own session
    [ "$ORPHAN_SID" = "$SESSION_ID" ] && continue
    # Skip if .start exists (normal session — Phase 1/2 handle these)
    [ -f "$TIMER_DIR/${ORPHAN_SID}.start" ] && continue
    # Skip if .agent is <2 hours old (session may still be setting up)
    if [ "$(find "$AF" -mmin +120 2>/dev/null)" ]; then
      # Phase 3 only cleans non-start files (no .start exists for these orphans)
      rm -f "$TIMER_DIR/${ORPHAN_SID}".{agent,pid,override,calls,tdd}
    fi
  done
}
