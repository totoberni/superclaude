# Module: Nudge file detection + delivery + cleanup
# Reads: AGENT_NAME, NUDGE_DIR | Sets: NUDGE_FIRED

mod_nudge() {
  # Runs for ANY named agent, before the orch-only gate.
  # One-shot: read first line, extract digits only, delete file, emit JSON additionalContext.
  [ -n "$AGENT_NAME" ] || return 0

  NUDGE_FILE="$NUDGE_DIR/$AGENT_NAME"
  NUDGE_CHARS=$(head -1 "$NUDGE_FILE" 2>/dev/null | tr -cd '0-9') || true
  if [ -n "$NUDGE_CHARS" ]; then
    rm -f "$NUDGE_FILE"
    NUDGE_FIRED=true
    printf '{"additionalContext":"NUDGE from the user: Produce a status report NOW. Format: [NUDGE] %s | {elapsed} | {emoji}\\n{description, max %s chars: current task, status, blocker if any}\\nThen resume your work."}\n' "$AGENT_NAME" "$NUDGE_CHARS"
  elif [ -e "$NUDGE_FILE" ]; then
    # Empty, invalid, or non-file nudge entry — clean it up
    rm -rf "$NUDGE_FILE" 2>/dev/null || true
  fi
}
