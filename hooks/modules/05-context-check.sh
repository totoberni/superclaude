# Module: Context estimation — warns on large memory footprint at startup
# Reads: AGENT_NAME, SESSION_ID, TIMER_DIR
# Advisory only — never blocks tool calls. One-shot per session.

mod_context_check() {
  # Only for named agents
  [ -n "$AGENT_NAME" ] || return 0

  # Only fire within first 10 tool calls
  local COUNTER_FILE="$TIMER_DIR/${SESSION_ID}.calls"
  local CALL_COUNT=0
  if [ -f "$COUNTER_FILE" ]; then
    CALL_COUNT=$(cat "$COUNTER_FILE" 2>/dev/null | tr -cd '0-9')
    [ -z "$CALL_COUNT" ] && CALL_COUNT=0
  fi
  [ "$CALL_COUNT" -gt 10 ] && return 0

  # One-shot: skip if already warned this session
  local WARNED_FILE="$TIMER_DIR/${SESSION_ID}.context-warned"
  [ -f "$WARNED_FILE" ] && return 0

  # ── Determine agent class (strip trailing digits/hyphens/instance suffix) ──
  # orch-<project>-p1 -> orch, scaf2 -> scaf, meta -> meta
  local AGENT_CLASS
  AGENT_CLASS=$(echo "$AGENT_NAME" | sed 's/[-_][0-9]*$//; s/-[a-z]*[-_][a-z0-9]*$//' 2>/dev/null)
  # Fallback: strip trailing digits
  [ -z "$AGENT_CLASS" ] && AGENT_CLASS=$(echo "$AGENT_NAME" | sed 's/[0-9]*$//')
  [ -z "$AGENT_CLASS" ] && AGENT_CLASS="$AGENT_NAME"

  # ── Sum memory file sizes ──
  local TOTAL_BYTES=0
  local STALE_FILES=""
  local NOW
  NOW=$(date +%s)
  local THIRTY_DAYS=$((30 * 86400))
  local MEM_DIR="$HOME/.claude/agent-memory"

  # Helper: add file size + check staleness
  check_mem_file() {
    local filepath="$1"
    [ -f "$filepath" ] || return 0
    local size
    size=$(wc -c < "$filepath" 2>/dev/null || echo "0")
    [[ "$size" =~ ^[0-9]+$ ]] || size=0
    TOTAL_BYTES=$((TOTAL_BYTES + size))
    # Staleness check
    local mtime
    mtime=$(stat -c %Y "$filepath" 2>/dev/null) || return 0
    if [[ "$mtime" =~ ^[0-9]+$ ]] && [ $((NOW - mtime)) -gt $THIRTY_DAYS ]; then
      local basename_f
      basename_f=$(basename "$filepath")
      STALE_FILES="${STALE_FILES}${basename_f} ($(( (NOW - mtime) / 86400 ))d), "
    fi
  }

  # Current paths (pre-DIR-030)
  check_mem_file "$MEM_DIR/$AGENT_NAME/MEMORY.md"
  check_mem_file "$MEM_DIR/shared/global/ltm.md"

  # DIR-030 future paths (instance + class)
  check_mem_file "$MEM_DIR/instance/$AGENT_NAME/MEMORY.md"
  check_mem_file "$MEM_DIR/class/$AGENT_CLASS/mtm.md"

  # Shared project memory: scan all project files (they're auto-loaded)
  if [ -d "$MEM_DIR/shared/projects" ]; then
    for pfile in "$MEM_DIR/shared/projects"/*.md; do
      check_mem_file "$pfile"
    done
  fi

  # ── Estimate tokens and decide ──
  local EST_TOKENS=$((TOTAL_BYTES / 4))

  if [ "$EST_TOKENS" -gt 8000 ]; then
    touch "$WARNED_FILE"
    local MSG="Memory footprint ~${EST_TOKENS} estimated tokens. Consider running /compact-mem to reduce startup load."
    if [ -n "$STALE_FILES" ]; then
      # Trim trailing comma+space
      STALE_FILES="${STALE_FILES%, }"
      MSG="$MSG Stale files (>30d): $STALE_FILES"
    fi
    printf '{"additionalContext":"%s"}\n' "$MSG"
  elif [ -n "$STALE_FILES" ]; then
    # Token count OK but stale files exist — lighter warning
    touch "$WARNED_FILE"
    STALE_FILES="${STALE_FILES%, }"
    printf '{"additionalContext":"Memory staleness: %s — may contain outdated context."}\n' "$STALE_FILES"
  fi
}
