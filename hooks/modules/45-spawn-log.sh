# Module: Spawn logger — telemetry for subagent dispatch lifecycle
# Reads: TOOL_NAME, INPUT, SESSION_ID, TIMER_DIR, AGENT_NAME
# Logs every Agent tool call to ~/.claude/comms/_spawns.log for:
#   - /swarm-status (live BG visibility)
#   - /promote (pattern detection across waves)
#   - /autocommission lifecycle audits
#
# Format (TSV): timestamp \t parent-agent \t subagent_type \t description (≤80 chars)
#
# NOTE: this hook fires at PreToolUse — BEFORE the child is launched — so the
# child's agent_id does not yet exist here and cannot be logged. The rich,
# agent_id-correlated SPAWN record lives in agent-outcome.sh (PostToolUse, where
# tool_response.agentId is available) -> ~/.claude/comms/_spawns-rich.log. This
# hook is kept as the parent/type/description producer for _spawns.log only.

mod_spawn_log() {
  [ "$TOOL_NAME" = "Agent" ] || return 0

  local LOG_FILE="$HOME/.claude/comms/_spawns.log"
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || return 0
  touch "$LOG_FILE" 2>/dev/null || return 0

  # Parse subagent_type and description from tool_input JSON
  local SUBAGENT_TYPE DESCRIPTION PARENT_AGENT
  SUBAGENT_TYPE=$(echo "$INPUT" | jq -r '.tool_input.subagent_type // "unknown"' 2>/dev/null) || SUBAGENT_TYPE="unknown"
  DESCRIPTION=$(echo "$INPUT" | jq -r '.tool_input.description // ""' 2>/dev/null | head -c 80 | tr -d '\n\r\t') || DESCRIPTION=""

  # Resolve parent agent name (cached by 00-parse.sh; fall back to global)
  PARENT_AGENT="${AGENT_NAME:-}"
  if [ -z "$PARENT_AGENT" ] && [ -f "$TIMER_DIR/${SESSION_ID}.agent" ]; then
    PARENT_AGENT=$(cat "$TIMER_DIR/${SESSION_ID}.agent" 2>/dev/null || echo "")
  fi
  [ -z "$PARENT_AGENT" ] && PARENT_AGENT="unknown"

  printf '%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$PARENT_AGENT" \
    "$SUBAGENT_TYPE" \
    "$DESCRIPTION" \
    >> "$LOG_FILE" 2>/dev/null || true
}
