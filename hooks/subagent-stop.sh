#!/usr/bin/env bash
# Hook: subagent-stop
# Event: SubagentStop (when a spawned worker exits)
# Purpose: log worker exit + outcome. Pairs with the Agent-tool spawn record in
# agent-outcome.sh (PostToolUse) — together they bracket every worker's lifetime.
#
# Three writes happen here (fail-safe, never blocks), across two log files:
#   1. _spawns.log      — legacy EXIT row, format unchanged for backward-compat
#                         with statusline-telemetry.sh + spawn-log-summary.sh.
#   2. _spawns-rich.log — rich EXIT row keyed on agent_id for the future
#                         statusline-subagent-monitor (correlates to the SPAWN
#                         row that agent-outcome.sh wrote with the same agent_id).
#   3. _spawns.log      : STOP forensics row (wf-skills W1.6), a third row
#                         type appended to the SAME file as (1). Marker STOP
#                         (not EXIT) keeps existing consumers untouched; see
#                         the STOP-row block near the end of this script for
#                         column layout and the agent_id/duration derivation.
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
# Sanitize at derivation (mirrors agent_id above) so every downstream row --
# legacy EXIT, rich EXIT, and STOP -- is protected from a crafted agent_type
# forging extra columns/rows via embedded TAB/NEWLINE bytes.
agent_type=$(printf '%s' "$agent_type" | tr -d '\t\n')

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

# ── STOP forensics row (_spawns.log) : wf-skills W1.6 ─────────────────────
# Adds a third row type to the SAME _spawns.log file. Marker is STOP (not
# EXIT) so statusline-telemetry.sh's col4=="EXIT" swarm-count logic and
# spawn-log-summary.sh's col2/3 reads both stay untouched by this row type.
# Columns 1-4 mirror the legacy EXIT row's positions (ts, parent, agent_type,
# marker); columns 5-8 are new for this row type:
#   <ts> <parent=""> <agent_type> STOP <agent_id> <duration_s> <status> <transcript_path>
# agent_id falls back to the literal string "unknown" for THIS row only (the
# rest of the script keeps its existing blank-default behavior unchanged).
# duration_s is derived by pairing on agent_id against the SPAWN row that
# agent-outcome.sh wrote to _spawns-rich.log (the only existing writer that
# has agent_id at spawn time; 45-spawn-log.sh's START row in _spawns.log has
# none, per its own comment, so pairing cannot happen inside _spawns.log
# itself). status reuses the $outcome heuristic derived above; the envelope
# carries no dedicated stop-reason field. Every step is defensive: any
# missing piece collapses that column to empty, the row is still written,
# exit stays 0.
agent_id_display="$agent_id"
[ -z "$agent_id_display" ] && agent_id_display="unknown"

transcript_path=$(echo "$input" | jq -r '.agent_transcript_path // .transcript_path // ""' 2>/dev/null) || transcript_path=""

duration_s=""
if [ -n "$agent_id" ] && [ -f "$rich_log" ]; then
  spawn_ts=$(awk -F'\t' -v aid="$agent_id" '$2==aid && $4=="SPAWN"{t=$1} END{print t}' "$rich_log" 2>/dev/null) || spawn_ts=""
  if [ -n "$spawn_ts" ]; then
    spawn_epoch=$(date -u -d "$spawn_ts" +%s 2>/dev/null) || spawn_epoch=""
    stop_epoch=$(date -u -d "$ts" +%s 2>/dev/null) || stop_epoch=""
    if [ -n "$spawn_epoch" ] && [ -n "$stop_epoch" ] && [ "$stop_epoch" -ge "$spawn_epoch" ] 2>/dev/null; then
      duration_s=$(( stop_epoch - spawn_epoch ))
    fi
  fi
fi

# m1 fix (wf-skills review round 1, log-forgery): strip TAB/NEWLINE from the
# payload-derived fields before they enter the tab-delimited row, so a crafted
# field cannot forge extra columns/rows in _spawns.log. Row format and field
# order below are unchanged. agent_type is sanitized at derivation above (it
# feeds all three row types), so only the two STOP-specific fields need it
# here.
agent_id_display_safe=$(printf '%s' "$agent_id_display" | tr -d '\t\n')
transcript_path_safe=$(printf '%s' "$transcript_path" | tr -d '\t\n')

printf '%s\t%s\t%s\tSTOP\t%s\t%s\t%s\t%s\n' \
  "$ts" \
  "" \
  "$agent_type" \
  "$agent_id_display_safe" \
  "$duration_s" \
  "$outcome" \
  "$transcript_path_safe" \
  >> "$log_file" 2>/dev/null || true

exit 0
