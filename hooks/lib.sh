#!/usr/bin/env bash
# ~/.claude/hooks/lib.sh — shared helpers for hook modules
# Source via: . "$HOME/.claude/hooks/lib.sh" (from any hook script or module)
#
# Helpers:
#   1. get_bash_cmd       — extract Bash tool command from $INPUT JSON
#   2. walk_to_agent      — proc-tree walk to find `claude --agent <name>`
#   3. safe_int           — sanitize a value to a non-negative integer
#   4. rm_session_files   — clean up timer files for a given session id
#   5. emit_context       — emit JSON {additionalContext: ...} safely via jq
#   6. already_warned     — one-shot warning marker per (session_id, key)

# 1. Parse Bash command from tool_input JSON (used by 4+ modules)
get_bash_cmd() {
  echo "$1" | jq -r '.tool_input.command // ""' 2>/dev/null
}

# 2. Walk process tree to find Claude agent name (used by 00-parse + pre-compact)
# Args: $1=starting PID (default $$), $2=max depth (default 8)
# Side effects: when called from 00-parse.sh, callers may set CLAUDE_PID via the
# WALK_PID variable kept exported in the legacy code. This helper returns ONLY
# the agent name string on stdout. For PID capture, the parse module retains
# its inline walk (it needs both PID and name in lockstep).
walk_to_agent() {
  local pid="${1:-$$}" max_depth="${2:-8}" depth=0 cmdline
  while [ "$depth" -lt "$max_depth" ] && [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    pid=$(awk '{print $4}' /proc/"$pid"/stat 2>/dev/null || echo "1")
    [ "$pid" -le 1 ] && break
    cmdline=$(tr '\0' ' ' < /proc/"$pid"/cmdline 2>/dev/null || echo "")
    if echo "$cmdline" | grep -qP -- '--agent\s'; then
      echo "$cmdline" | grep -oP -- '--agent\s+\K\S+' || echo ""
      return 0
    fi
    depth=$((depth + 1))
  done
  echo ""
  return 1
}

# 3. Safe-int sanitization (used by 05/20/40)
safe_int() {
  local val
  val=$(echo "${1:-0}" | tr -cd '0-9')
  echo "${val:-0}"
}

# 4. Clean up session timer files (used by cleanup.sh + 40-gc 3x)
# Args: $1=session_id
# Note: chmods .start to 644 first because mod_timer set it to 444.
rm_session_files() {
  local sid="$1" timer_dir="${TIMER_DIR:-$HOME/.claude/session-timers}"
  [ -z "$sid" ] && return 0
  chmod 644 "$timer_dir/${sid}.start" 2>/dev/null || true
  rm -f "$timer_dir/${sid}".{start,agent,pid,override,calls,tdd,context-warned,baseline-stashed,commit-gate-warned,bootstrap-warned}
}

# 5. Emit JSON additionalContext safely (used by 5+ modules)
# Args: $1=context message string
# Uses jq for safe JSON encoding (handles quotes, newlines, etc.)
emit_context() {
  # Emit the full hookSpecificOutput envelope so additionalContext actually reaches
  # the model (the bare {additionalContext} form is ignored for PreToolUse). The
  # event name is read from the hook stdin ($INPUT); defaults to PreToolUse. jq-safe.
  local evt
  evt=$(printf '%s' "${INPUT:-}" | jq -r '.hook_event_name // empty' 2>/dev/null)
  [ -n "$evt" ] || evt="PreToolUse"
  jq -nc --arg ctx "$1" --arg evt "$evt" \
    '{hookSpecificOutput: {hookEventName: $evt, additionalContext: $ctx}}'
}

# 6. One-shot warning marker (per audit O15)
# Args: $1=session_id, $2=warning_key (e.g. "commit-gate", "bootstrap")
# Returns 0 if warning already shown (caller should skip), 1 if not (caller should warn).
already_warned() {
  local sid="$1" key="$2" timer_dir="${TIMER_DIR:-$HOME/.claude/session-timers}"
  [ -z "$sid" ] && return 1
  local marker="$timer_dir/${sid}.${key}-warned"
  [ -f "$marker" ] && return 0
  touch "$marker" 2>/dev/null || true
  return 1
}
