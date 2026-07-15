# Guard: 50-heuristics, F7 (flag-only heuristic partials; design/enforcement-gap-ledger.md
# Family 7 #24/#25). Both invariants are honest PARTIALS: cleanly buildable enough to flag,
# not reliably buildable enough to block. Neither function in this file ever calls
# guard_block; both are advisory nudges only (PHASE2-CONTRACT sec 2/6).
#
# guard_heuristics (PreToolUse, #24): a Grep call carrying a head_limit field is flagged as an
# advisory (rules/20-tool-conventions.md Sweep Verification). Detecting "does this grep GATE a
# claim" (absence/presence check, closure sweep) is not reliably buildable from tool_input
# alone, so this only flags head_limit PRESENCE, nothing more.
#
# guardpost_heuristics (PostToolUse, #25): a per-session tool-call counter under $TMPDIR. Once
# the count crosses 20, warns ONCE that the ~20-24 sonnet-truncation threshold (rules/13) is
# close. "Force model" would need re-dispatch cooperation from the caller, so this is flag-only
# too. Best-effort: fails open (no warn, no error) if session_id is unavailable.

GUARD_MODE_HEURISTICS=warn

guard_heuristics() {
  [ "${GUARD_TOOL:-}" = "Grep" ] || return 0
  local head_limit
  head_limit=$(guard_field "head_limit")
  [ -n "$head_limit" ] || return 0
  guard_warn "head-limited grep: for a gating absence/presence claim use FULL enumeration or grep -c (rules/20 Sweep Verification)"
}

guardpost_heuristics() {
  guard_kill_switch && return 0
  command -v jq >/dev/null 2>&1 || return 0

  local sid
  sid=$(printf '%s' "${GUARD_STDIN:-}" | jq -r '.session_id // empty' 2>/dev/null)
  [ -n "$sid" ] || return 0

  # Kept under $TMPDIR (not the repo, not ~/.claude/session-timers) so this counter is
  # purely advisory scratch state, distinct from the session-timer's own .calls file.
  local counter_dir="${TMPDIR:-/tmp}/superclaude-guard-heuristics"
  mkdir -p "$counter_dir" 2>/dev/null || return 0
  local counter_file="$counter_dir/${sid}.calls"
  local warned_file="$counter_dir/${sid}.warned"

  local count
  count=$(tr -cd '0-9' <"$counter_file" 2>/dev/null)
  [ -n "$count" ] || count=0
  count=$((count + 1))
  printf '%s' "$count" >"$counter_file" 2>/dev/null || return 0

  [ "$count" -gt 20 ] || return 0
  [ -f "$warned_file" ] && return 0
  touch "$warned_file" 2>/dev/null || true
  guard_warn "approaching the ~20-24 tool-call sonnet-truncation threshold; heavy tasks should use model opus (rules/13 worker model split)"
}
