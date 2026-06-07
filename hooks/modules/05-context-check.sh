# Module: Context compaction warning — warns when used tokens approach auto-compact threshold
# Reads: AGENT_NAME, SESSION_ID, TIMER_DIR
# Advisory only — never blocks tool calls. One-shot per session.

mod_context_check() {
  # Only for named agents
  [ -n "$AGENT_NAME" ] || return 0

  local CONTEXT_FILE="$HOME/.claude/.context-latest.json"

  # If the statusline hasn't written the file yet, that's normal — skip silently
  [ -f "$CONTEXT_FILE" ] || return 0

  # Parse used_tokens and window_size via jq; any parse failure → silent exit
  local USED_TOKENS WINDOW_SIZE USED_PCT
  USED_TOKENS=$(jq -e '.used_tokens' "$CONTEXT_FILE" 2>/dev/null) || return 0
  WINDOW_SIZE=$(jq -e '.window_size' "$CONTEXT_FILE" 2>/dev/null) || return 0
  USED_PCT=$(jq -r '.used_pct' "$CONTEXT_FILE" 2>/dev/null) || return 0

  # Validate both are integers
  USED_TOKENS=$(safe_int "$USED_TOKENS")
  WINDOW_SIZE=$(safe_int "$WINDOW_SIZE")
  [ "$USED_TOKENS" -gt 0 ] 2>/dev/null || return 0
  [ "$WINDOW_SIZE" -gt 0 ] 2>/dev/null || return 0

  # Auto-compact fires at window_size - 33000
  local AUTOCOMPACT=$(( WINDOW_SIZE - 33000 ))

  # Warn when within 20000 tokens of auto-compact threshold
  local WARN_AT=$(( AUTOCOMPACT - 20000 ))
  [ "$USED_TOKENS" -ge "$WARN_AT" ] || return 0

  # One-shot: skip if already warned this session
  local WARNED_FILE="$TIMER_DIR/${SESSION_ID}.context-compact-warned"
  [ -f "$WARNED_FILE" ] && return 0
  touch "$WARNED_FILE" 2>/dev/null || true

  emit_context "Context ~${USED_TOKENS}/${WINDOW_SIZE} tok (${USED_PCT}%); auto-compact near ${AUTOCOMPACT}. Checkpoint state + stash recovery now."
}
