#!/usr/bin/env bash
# Hook: agent-outcome
# Event: PostToolUse for tool_name == Agent
# Purpose: Classify the just-returned subagent response as OK / TRUNCATED / FAILED / UNKNOWN
#          and append a TSV row to ~/.claude/comms/_outcomes.log.
# Soft: never blocks, never mutates anything except the outcomes log file.
#
# Driven by V-001 retro: WORKER-TRUNCATED surfaced 4+ times in one session as a new failure
# mode. Without telemetry we cannot quantify it across runs. Sister hook to spawn logging
# (modules/45-spawn-log.sh) and recovery skill (/recover-truncated).
#
# Standalone PostToolUse hook (NOT sourced as a module). Wired in settings.json
# alongside comms-schema-lint.sh. It exits 0 always.

set -uo pipefail

INPUT=$(cat)

# Only act on Agent tool returns
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
[ "$TOOL_NAME" = "Agent" ] || exit 0

OUTCOMES_LOG="$HOME/.claude/comms/_outcomes.log"
mkdir -p "$(dirname "$OUTCOMES_LOG")" 2>/dev/null || exit 0

# Initialize log with header comment if empty / missing
if [ ! -s "$OUTCOMES_LOG" ]; then
  {
    printf '# Agent outcome log — TSV: timestamp\\tparent_agent\\tsubagent_type\\tdescription\\toutcome\\tlast_120_chars\n'
    printf '# Outcomes: OK | TRUNCATED | FAILED | UNKNOWN\n'
    printf '# Source hook: ~/.claude/hooks/agent-outcome.sh\n'
  } >> "$OUTCOMES_LOG" 2>/dev/null || true
fi

# Parse spawn metadata from tool_input
SUBAGENT_TYPE=$(echo "$INPUT" | jq -r '.tool_input.subagent_type // "unknown"' 2>/dev/null || echo "unknown")
DESCRIPTION=$(echo "$INPUT" | jq -r '.tool_input.description // ""' 2>/dev/null | head -c 120 | tr -d '\n\r\t' || echo "")

# ── Rich SPAWN record (_spawns-rich.log) ──────────────────────────────────
# PostToolUse on Agent is the FIRST point the spawned child's agent_id exists
# (the PreToolUse hook 45-spawn-log.sh fires before launch, so it has no child
# id and cannot be correlated to an EXIT). The child id is in tool_response:
#   tool_response.agentId   e.g. "a1c961bf042876f70"
#   tool_response.status    "async_launched" for run_in_background spawns
# This same id reappears as `agent_id` in the SubagentStop envelope, so it is
# the SPAWN↔EXIT correlation key. Schema (TSV, shared with subagent-stop.sh):
#   <iso_ts> \t <agent_id> \t <subagent_type> \t SPAWN \t <short_desc> \t <outcome>
# outcome is empty on SPAWN (the worker has not finished); the EXIT row carries it.
RICH_LOG="$HOME/.claude/comms/_spawns-rich.log"
CHILD_AGENT_ID=$(echo "$INPUT" | jq -r '.tool_response.agentId // ""' 2>/dev/null || echo "")
CHILD_AGENT_ID=$(printf '%s' "$CHILD_AGENT_ID" | tr -cd 'a-zA-Z0-9_-')
SHORT_DESC=$(printf '%s' "$DESCRIPTION" | cut -c1-40)
printf '%s\t%s\t%s\tSPAWN\t%s\t\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$CHILD_AGENT_ID" \
  "$SUBAGENT_TYPE" \
  "$SHORT_DESC" \
  >> "$RICH_LOG" 2>/dev/null || true

# Resolve parent agent name (cached by 00-parse.sh via $TIMER_DIR/<session>.agent)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""' 2>/dev/null || echo "")
SESSION_ID=$(printf '%s' "$SESSION_ID" | tr -cd 'a-zA-Z0-9_-')
PARENT_AGENT=""
if [ -n "$SESSION_ID" ] && [ -f "$HOME/.claude/session-timers/${SESSION_ID}.agent" ]; then
  PARENT_AGENT=$(cat "$HOME/.claude/session-timers/${SESSION_ID}.agent" 2>/dev/null || echo "")
fi
[ -z "$PARENT_AGENT" ] && PARENT_AGENT="unknown"

# Extract the worker's visible output text from tool_response. The shape Claude Code uses for
# Agent tool returns varies across versions; try the common paths and fall back to the raw blob.
# Be type-aware: tool_response may be a string, an array of content blocks, or an object.
RESPONSE_TEXT=$(echo "$INPUT" | jq -r '
  (.tool_response // "")
  | if type == "string" then .
    elif type == "array" then map(.text? // (if type == "string" then . else (. | tojson) end)) | join("\n")
    elif type == "object" then
      (.content // .text // .output // .)
      | if type == "string" then .
        elif type == "array" then map(.text? // (if type == "string" then . else (. | tojson) end)) | join("\n")
        elif type == "object" then (.text? // (. | tojson))
        else . end
    else . end
' 2>/dev/null || echo "")

# Tail-bound: if larger than 50KB, only inspect the last 5KB
RESP_LEN=${#RESPONSE_TEXT}
if [ "$RESP_LEN" -gt 51200 ]; then
  RESPONSE_TEXT="${RESPONSE_TEXT: -5120}"
fi

# Last 120 chars (sanitized) for log row — strips control chars
LAST_120=$(printf '%s' "$RESPONSE_TEXT" | tail -c 120 | tr -d '\n\r\t' | head -c 120)
[ -z "$LAST_120" ] && LAST_120="(empty)"

# ── Classification ──
# Order: FAILED > TRUNCATED > OK > UNKNOWN (most specific wins)
OUTCOME="UNKNOWN"

# 1. FAILED — explicit error indicators near end of output (last 2KB)
LAST_2K=$(printf '%s' "$RESPONSE_TEXT" | tail -c 2048)
if echo "$LAST_2K" | grep -qE '(^|\b)(Error:|Failed:|FAILED:|BLOCKED:|Traceback \(most recent|fatal:|panic:)' 2>/dev/null; then
  OUTCOME="FAILED"
fi

# 2. OK — closing markers present anywhere (search whole tail)
if [ "$OUTCOME" = "UNKNOWN" ]; then
  if echo "$RESPONSE_TEXT" | grep -qE '^## (Verification|Files changed|Diff|Output|Summary|Result|Done|Notes)' 2>/dev/null; then
    OUTCOME="OK"
  fi
fi

# 3. TRUNCATED — ends with incomplete-sentence patterns and no closing markers.
#    Truncation can happen at ANY length (not just >500), so check regardless of size
#    but require at least a non-trivial output (>80 chars — below this, no closing
#    markers is normal for one-line answers).
if [ "$OUTCOME" = "UNKNOWN" ] && [ "$RESP_LEN" -gt 80 ]; then
  # Common truncation tails seen in V-001: "Let me ...", ends mid-sentence (no punctuation),
  # "OK let me think DIFFERENTLY...", "what did that touch?", "Let me retry:"
  TAIL_TRIM=$(printf '%s' "$RESPONSE_TEXT" | tail -c 240 | sed 's/[[:space:]]*$//')
  TAIL_LAST_CHAR=$(printf '%s' "$TAIL_TRIM" | tail -c 1)
  if echo "$TAIL_TRIM" | grep -qiE '(let me (try|retry|check|see|look|read|run|think|investigate|verify|examine|inspect|continue|figure|do)|what did that touch|wait,? let me|hmm,? |actually,? |OK let me)' 2>/dev/null; then
    OUTCOME="TRUNCATED"
  elif [ -n "$TAIL_LAST_CHAR" ] && ! printf '%s' "$TAIL_LAST_CHAR" | grep -qE '[.!?\)\]\}"`>]' 2>/dev/null; then
    # Ends mid-word/mid-sentence without terminal punctuation AND no closing markers anywhere — likely truncated
    OUTCOME="TRUNCATED"
  fi
fi

# 4. UNKNOWN remains for ambiguous outputs. For legitimately short outputs (e.g., recon-only
#    w-explorer returning 1-line answer) treat as OK if NO truncation indicators fired above.
#    Per directive: "Some agents legitimately end without ## Verification — treat as OK if NO truncation indicators".
if [ "$OUTCOME" = "UNKNOWN" ] && [ "$RESP_LEN" -gt 0 ]; then
  # No closing markers, no error, no truncation indicators → treat as OK
  OUTCOME="OK"
fi

# Append TSV row (no sub-shell failure should ever propagate to caller)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$TIMESTAMP" \
  "$PARENT_AGENT" \
  "$SUBAGENT_TYPE" \
  "$DESCRIPTION" \
  "$OUTCOME" \
  "$LAST_120" \
  >> "$OUTCOMES_LOG" 2>/dev/null || true

exit 0
