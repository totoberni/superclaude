#!/bin/bash
# Session timer hook: thin dispatcher that sources modular components.
# Registered for SessionStart and PreToolUse in settings.json.
#
# Modules (hooks/modules/):
#   00-parse.sh     — JSON parsing + agent detection + caching
#   05-context-check.sh — Memory footprint estimation + staleness (first 10 calls)
#   10-nudge.sh     — Nudge file detection + delivery + cleanup
#   14-agent-thinking-nudge.sh — V-001 lesson: soft nudge when matrix prescribes thinking keyword
#   15-baseline-stash.sh — R-2 mitigation: stash baseline for /commit false repos
#   20-counter.sh   — Tool call efficiency counter + TDD awareness
#   25-commit-gate.sh — Commit quality gate (conventional format + push reminder)
#   30-timer.sh     — Session timer lifecycle (warn/grace/block)
#   40-gc.sh        — GC Phase 1/2/3 (stale/dead/orphan cleanup)
#   45-spawn-log.sh — Telemetry: log Agent tool calls to comms/_spawns.log
#   50-bootstrap.sh — Bootstrap freshness check (SessionStart only)
#
# Shared vars: SESSION_ID, TOOL_NAME, AGENT_NAME, TIMER_DIR, NUDGE_DIR, NUDGE_FIRED
# Exit codes: 0 = allow, 2 = block

set -uo pipefail

INPUT=$(cat)
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMER_DIR="$HOME/.claude/session-timers"
NUDGE_DIR="$HOME/.claude/nudge"
NUDGE_FIRED=false
SESSION_ID=""
AGENT_NAME=""
TOOL_NAME=""
START_FILE=""
OVERRIDE_FILE=""
AGENT_FILE=""
PID_FILE=""
CLAUDE_PID=""

mkdir -p "$TIMER_DIR"

# Source shared helpers (get_bash_cmd, walk_to_agent, safe_int,
# rm_session_files, emit_context, already_warned).
. "$HOOK_DIR/lib.sh" 2>/dev/null || { echo "WARN: lib.sh not found at $HOOK_DIR/lib.sh" >&2; }

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
run_mod mod_skm_session
run_mod mod_context_check
run_mod mod_nudge
run_mod mod_thinking_nudge
run_mod mod_baseline_stash
run_mod mod_spawn_log
run_mod mod_counter
run_mod mod_commit_gate
run_mod mod_notebook_guard
run_mod mod_timer

exit 0
