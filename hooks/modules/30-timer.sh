# Module: Session timer lifecycle — start recording, exemptions, enforcement
# Reads: AGENT_NAME, SESSION_ID, START_FILE, OVERRIDE_FILE, TOOL_NAME, INPUT, TIMER_DIR
#
# Timeline:
#   0-45 min  — normal operation
#   45-48 min — WARNING (non-blocking, tells agent to wrap up)
#   48-53 min — GRACE PERIOD (only shutdown ops: state/report/memory writes, git commits)
#   53+ min   — HARD BLOCK (all tool calls rejected)
#
# Exempt: meta agent, bare claude (no --agent).
# Override: touch ~/.claude/session-timers/<session_id>.override

mod_timer() {
  # ── First call: record start time for ALL named agents (including meta) ──
  if [ -n "$AGENT_NAME" ] && [ ! -f "$START_FILE" ]; then
    date +%s > "$START_FILE"
    chmod 444 "$START_FILE" 2>/dev/null || true
  fi

  # ── Exempt: meta and bare claude only ──
  # All other named agents (orch-*, scaf, workers) get timer enforcement.
  if [ -z "$AGENT_NAME" ] || [ "$AGENT_NAME" = "meta" ]; then
    exit 0
  fi

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
  WARN_SECONDS=$((45 * 60))   # 45 min
  SOFT_SECONDS=$((48 * 60))   # 48 min — grace period starts
  HARD_SECONDS=$((53 * 60))   # 53 min — hard block

  MINUTES=$((ELAPSED / 60))

  # ── Hard block (48+ min) ──
  if [ $ELAPSED -gt $HARD_SECONDS ]; then
    echo "HARD SESSION LIMIT (${MINUTES} min). ALL tool calls blocked." >&2
    echo "Run /compact to preserve context, then start a new session or override: touch $OVERRIDE_FILE" >&2
    # ── Circuit-breaker counter ──
    BLOCKED_COUNT_FILE="${TIMER_DIR}/${SESSION_ID}.blocked-count"
    BLOCKED_COUNT=$(cat "$BLOCKED_COUNT_FILE" 2>/dev/null || echo "0")
    if ! [[ "$BLOCKED_COUNT" =~ ^[0-9]+$ ]]; then BLOCKED_COUNT=0; fi
    BLOCKED_COUNT=$(( BLOCKED_COUNT + 1 ))
    echo "$BLOCKED_COUNT" > "$BLOCKED_COUNT_FILE" 2>/dev/null || true
    if [ "$BLOCKED_COUNT" -gt 3 ]; then
      echo "Repeated hard-blocks — run /compact or start a fresh session now." >&2
    fi
    exit 2
  fi

  # ── Grace period (40-48 min) — only shutdown ops allowed ──
  if [ $ELAPSED -gt $SOFT_SECONDS ]; then
    REMAINING=$(( (HARD_SECONDS - ELAPSED) / 60 ))
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
        COMMAND=$(get_bash_cmd "$INPUT")
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
      echo "  Run /compact now to preserve context before the hard block." >&2
      exit 0
    else
      echo "GRACE PERIOD (${MINUTES} min). Tool call BLOCKED — not a shutdown operation." >&2
      echo "Allowed: Write/Edit to ~/.claude/*, git add/commit/status, Read/Glob/Grep." >&2
      echo "Run /compact to preserve context. Override: touch $OVERRIDE_FILE" >&2
      exit 2
    fi
  fi

  # ── Warning (35-40 min) ──
  if [ $ELAPSED -gt $WARN_SECONDS ]; then
    REMAINING=$(( (SOFT_SECONDS - ELAPSED) / 60 ))
    echo "TIME WARNING (${MINUTES} min, ~${REMAINING} min until grace period)." >&2
    echo "Wrap up current task. After 48 min: shutdown ops only. After 53 min: blocked." >&2
  fi

  exit 0
}
