#!/bin/bash
# Session timer hook: thin dispatcher that sources modular components.
# Registered for SessionStart and PreToolUse in settings.json.
#
# Modules (hooks/modules/):
#   00-parse.sh     — JSON parsing + agent detection + caching
#   05-context-check.sh — Memory footprint estimation + staleness (first 10 calls)
#   10-nudge.sh     — Nudge file detection + delivery + cleanup
#   20-counter.sh   — Tool call efficiency counter + TDD awareness
#   25-commit-gate.sh — Commit quality gate (conventional format + push reminder)
#   30-timer.sh     — Session timer lifecycle (warn/grace/block)
#   40-gc.sh        — GC Phase 1/2/3 (stale/dead/orphan cleanup)
#   50-bootstrap.sh — Bootstrap freshness check (SessionStart only)
#
# Shared vars: SESSION_ID, TOOL_NAME, AGENT_NAME, TIMER_DIR, NUDGE_DIR, NUDGE_FIRED
# Exit codes: 0 = allow, 2 = block

set -euo pipefail

INPUT=$(cat)
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMER_DIR="$HOME/.claude/session-timers"
NUDGE_DIR="$HOME/.claude/nudge"
NUDGE_FIRED=false
SESSION_ID=""
AGENT_NAME=""
TOOL_NAME=""

mkdir -p "$TIMER_DIR"

# Source all modules (defines functions only)
LC_COLLATE=C
for mod in "$HOOK_DIR/modules/"[0-9]*.sh; do
  if [ -f "$mod" ]; then
    source "$mod"
  else
    echo "WARN: hook module not found: $mod" >&2
  fi
done

# Run module function if defined, skip if not
run_mod() {
  if declare -F "$1" >/dev/null 2>&1; then
    "$1"
  fi
}

# Execute modules in order
run_mod mod_parse
run_mod mod_gc
run_mod mod_bootstrap_check
run_mod mod_context_check
run_mod mod_nudge
run_mod mod_counter
run_mod mod_commit_gate
run_mod mod_timer

exit 0
