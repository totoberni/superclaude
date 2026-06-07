#!/usr/bin/env bash
# Hook: subagent-stop
# Event: SubagentStop (when a spawned worker exits)
# Purpose: log worker exit + outcome. Pairs with the Agent-tool spawn record in
# agent-outcome.sh (PostToolUse) — together they bracket every worker's lifetime.
#
# Two logs are written (fail-safe, never blocks):
#   1. _spawns.log      — legacy EXIT row, format unchanged for backward-compat
#                         with statusline-telemetry.sh + spawn-log-summary.sh.
#   2. _spawns-rich.log — rich EXIT row keyed on agent_id for the future
#                         statusline-subagent-monitor (correlates to the SPAWN
#                         row that agent-outcome.sh wrote with the same agent_id).
#
# DISCOVERED SubagentStop stdin schema (CC 2.1.159 zod, authoritative):
#   hook_event_name        "SubagentStop"
#   stop_hook_active        bool
#   agent_id                string  <- the child's id (== tool_response.agentId at spawn)
#   agent_transcript_path   string
#   agent_type              string  <- the subagent_type (e.g. "w-implementer")
#   last_assistant_message  string  <- final assistant text (no transcript parse needed!)
#   + base fields: session_id, transcript_path, cwd, permission_mode, agent_id, agent_type
# NOTE: the legacy field names this hook previously used (subagent_id / subagent_type /
# parent_session_id) DO NOT EXIST in the envelope — that is why every EXIT row in
# _spawns.log was blank/unknown. The names below are the real ones.

set -uo pipefail

input=$(cat)

# Real field names (see schema above). Fall back to legacy names defensively in
# case a future CC version reintroduces them; empty string if neither present.
agent_id=$(echo "$input" | jq -r '.agent_id // .subagent_id // ""' 2>/dev/null) || agent_id=""
agent_id=$(printf '%s' "$agent_id" | tr -cd 'a-zA-Z0-9_-')
agent_type=$(echo "$input" | jq -r '.agent_type // .subagent_type // "unknown"' 2>/dev/null) || agent_type="unknown"
[ -z "$agent_type" ] && agent_type="unknown"

log_file="$HOME/.claude/comms/_spawns.log"
rich_log="$HOME/.claude/comms/_spawns-rich.log"
mkdir -p "$(dirname "$log_file")" 2>/dev/null || exit 0

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── Legacy EXIT row (_spawns.log) ─────────────────────────────────────────
# Format preserved exactly: ts \t parent \t subagent_type \t EXIT \t agent_id.
# statusline-telemetry.sh keys the live "active swarm" count off col4 == "EXIT";
# spawn-log-summary.sh reads cols 2/3. Both keep working. Parent is unknown at
# SubagentStop (the envelope carries no parent agent name), so col2 stays blank
# as before — agent_type now populates col3 instead of the previous "unknown".
printf '%s\t%s\t%s\tEXIT\t%s\n' \
  "$ts" \
  "" \
  "$agent_type" \
  "$agent_id" \
  >> "$log_file" 2>/dev/null || true

# ── Outcome classification from last_assistant_message ────────────────────
# Available directly in the envelope (no transcript read). ok | fail | "".
# Conservative: only assert "fail" on explicit error markers near the tail;
# otherwise "ok" when there is non-trivial final text; empty when truly silent.
last_msg=$(echo "$input" | jq -r '.last_assistant_message // ""' 2>/dev/null) || last_msg=""
outcome=""
if [ -n "$last_msg" ]; then
  tail_msg=$(printf '%s' "$last_msg" | tail -c 2048)
  if printf '%s' "$tail_msg" | grep -qE '(^|\b)(Error:|Failed:|FAILED:|BLOCKED:|Blocker:|Traceback \(most recent|fatal:|panic:|### Blockers)' 2>/dev/null; then
    outcome="fail"
  else
    outcome="ok"
  fi
fi

# ── Rich EXIT row (_spawns-rich.log) ──────────────────────────────────────
# Schema (shared with agent-outcome.sh SPAWN rows):
#   <iso_ts> \t <agent_id> \t <subagent_type> \t EXIT \t <short_desc> \t <outcome>
# short_desc is empty here — SubagentStop carries no description; pair to the
# SPAWN row via agent_id to recover it. outcome: ok | fail | "".
printf '%s\t%s\t%s\tEXIT\t\t%s\n' \
  "$ts" \
  "$agent_id" \
  "$agent_type" \
  "$outcome" \
  >> "$rich_log" 2>/dev/null || true

exit 0
