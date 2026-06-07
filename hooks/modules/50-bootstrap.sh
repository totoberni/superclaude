# Module: Bootstrap freshness check (SessionStart only)
# Warns if bootstrap.md is older than directives.md (may be stale)
# Reads: AGENT_NAME, START_FILE

mod_bootstrap_check() {
  # Only at session start (before .start file exists)
  [ -f "$START_FILE" ] && return 0
  # Only for agents with comms dirs
  [ -n "$AGENT_NAME" ] || return 0
  local comms_dir="$HOME/.claude/comms/$AGENT_NAME"
  [ -d "$comms_dir" ] || return 0
  local boot_file="$comms_dir/bootstrap.md"
  local dir_file="$comms_dir/directives.md"
  [ -f "$boot_file" ] && [ -f "$dir_file" ] || return 0
  local boot_ts dir_ts
  boot_ts=$(stat -c %Y "$boot_file" 2>/dev/null) || return 0
  dir_ts=$(stat -c %Y "$dir_file" 2>/dev/null) || return 0
  if [[ "$dir_ts" =~ ^[0-9]+$ ]] && [[ "$boot_ts" =~ ^[0-9]+$ ]] && [ "$dir_ts" -gt "$boot_ts" ]; then
    # One-shot per session (audit O15): the SessionStart guard above already
    # limits this to first call, but additional safety against re-entry —
    # marker survives across SessionStart re-runs in the same session id.
    if ! already_warned "$SESSION_ID" "bootstrap"; then
      echo "WARN: bootstrap.md older than directives.md for $AGENT_NAME — may be stale. Ask Meta to update." >&2
    fi
  fi
}
