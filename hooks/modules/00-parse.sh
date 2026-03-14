# Module: JSON parsing, session ID sanitization, agent detection + caching
# Sets: SESSION_ID, TOOL_NAME, AGENT_NAME, START_FILE, OVERRIDE_FILE, AGENT_FILE, PID_FILE

mod_parse() {
  SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null) || SESSION_ID="unknown"
  # Sanitize session ID — prevent path traversal and shell injection
  SESSION_ID=$(printf '%s' "$SESSION_ID" | tr -cd 'a-zA-Z0-9_-')
  [ -z "$SESSION_ID" ] && SESSION_ID="unknown"

  TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null) || TOOL_NAME=""

  START_FILE="$TIMER_DIR/${SESSION_ID}.start"
  OVERRIDE_FILE="$TIMER_DIR/${SESSION_ID}.override"
  AGENT_FILE="$TIMER_DIR/${SESSION_ID}.agent"
  PID_FILE="$TIMER_DIR/${SESSION_ID}.pid"

  # ── Detect agent type + parent PID (cached after first detection) ──
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
}
