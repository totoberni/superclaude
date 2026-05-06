# Module: Tool call efficiency counter + TDD awareness
# Reads: AGENT_NAME, SESSION_ID, TIMER_DIR, NUDGE_FIRED, TOOL_NAME, INPUT

mod_counter() {
  [ -n "$AGENT_NAME" ] && [ "$AGENT_NAME" != "meta" ] || return 0

  # ── Call counter ──
  COUNTER_FILE="$TIMER_DIR/${SESSION_ID}.calls"
  CALL_COUNT=0
  [ -f "$COUNTER_FILE" ] && CALL_COUNT=$(cat "$COUNTER_FILE" 2>/dev/null | tr -cd '0-9')
  [ -z "$CALL_COUNT" ] && CALL_COUNT=0
  CALL_COUNT=$((CALL_COUNT + 1))
  echo "$CALL_COUNT" > "$COUNTER_FILE"

  # ── TDD counter: track edits without test runs ──
  TDD_FILE="$TIMER_DIR/${SESSION_ID}.tdd"
  TDD_COUNT=0
  [ -f "$TDD_FILE" ] && TDD_COUNT=$(cat "$TDD_FILE" 2>/dev/null | tr -cd '0-9')
  [ -z "$TDD_COUNT" ] && TDD_COUNT=0

  if [ "$TOOL_NAME" = "Edit" ] || [ "$TOOL_NAME" = "Write" ]; then
    TDD_COUNT=$((TDD_COUNT + 1))
    echo "$TDD_COUNT" > "$TDD_FILE"
  elif [ "$TOOL_NAME" = "Bash" ]; then
    BASH_CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null) || BASH_CMD=""
    if echo "$BASH_CMD" | grep -qE '(pytest|python.*-m\s+pytest|npm\s+test|npx\s+jest|make\s+test|cargo\s+test|go\s+test)'; then
      TDD_COUNT=0
      echo "0" > "$TDD_FILE"
    fi
  fi

  # ── Nudge mutual exclusivity: nudge wins everything ──
  [ "$NUDGE_FIRED" = true ] && return 0

  # TDD reminder at 5+ edits without test (fires once per threshold crossing)
  if [[ "$TDD_COUNT" =~ ^[0-9]+$ ]] && [ "$TDD_COUNT" -ge 5 ] && [ $((TDD_COUNT % 5)) -eq 0 ]; then
    printf '{"additionalContext":"TDD reminder: %d file edits since last test run. Consider running tests before more changes. Use /test-scaffold if the project lacks test infrastructure."}\n' "$TDD_COUNT"
    return 0
  fi

  # Efficiency check every 25 calls
  if [ $((CALL_COUNT % 25)) -eq 0 ] && [ "$CALL_COUNT" -gt 0 ]; then
    printf '{"additionalContext":"Efficiency check (%d tool calls this session). Consider: (1) delegate parallelizable work to workers, (2) use offset/limit for large files, (3) batch related operations, (4) let worker failures stand -- do not redo their work yourself."}\n' "$CALL_COUNT"
  fi
}
