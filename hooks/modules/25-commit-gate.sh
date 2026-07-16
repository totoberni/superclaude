# Module: Commit quality gate: push reminder
# Reads: TOOL_NAME, INPUT, NUDGE_FIRED
# Soft enforcement ONLY, never blocks commits

mod_commit_gate() {
  [ "$TOOL_NAME" = "Bash" ] || return 0
  [ "$NUDGE_FIRED" = true ] && return 0

  BASH_CMD=$(get_bash_cmd "$INPUT")
  [ -z "$BASH_CMD" ] && return 0

  # ── Push reminder ──
  if echo "$BASH_CMD" | grep -qE '^\s*git(\s+-C\s+\S+)?\s+push'; then
    emit_context "Push detected. Reminder: verify with the user before pushing. Never push without explicit instruction."
    return 0
  fi
}
