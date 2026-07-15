# Guard: 70-wrong-tool: WRONG-TOOL / suboptimal-tooling detector (design/DECISION-DOC.md
# section 4; enforcement-gap-ledger.md Family 7 context; the mechanical replacement for
# the rejected advisory R-6). FLAG-level ONLY: using the wrong tool is an overridable
# judgement, not a safety breach, so this file NEVER calls guard_block; every finding is a
# guard_warn. GUARD_MODE_WRONG_TOOL=warn.
#
# Two halves, both mechanical (no advisory-guarding-advisory):
#
# Half A - governed-shape-without-machinery. The session marker that says "governed
#   machinery WAS engaged this session" is dropped MECHANICALLY by _wrong_tool_marker_drop
#   whenever GUARD_TOOL==Skill and tool_input.skill names a governance skill (design/
#   DECISION-DOC.md sec 4, r1/M3: hook-driven on tool_name==Skill, NOT by skill prose).
#   00-parse.sh already exposes TOOL_NAME / GUARD_TOOL, so this is buildable. The detector
#   then recognizes a governed SHAPE plus the ABSENCE of the matching marker:
#     (i)  >=4 WebSearch/WebFetch in a session with no research marker -> warn
#          "research-saturation shape; use /research or /.deep-research (dot-escaped)".
#     (ii) a >=3-wide parallel Agent batch with no swarm-dispatch marker -> warn
#          "wave/parallel shape without /swarm-dispatch".
#   The reviewer-dispatch-without-ledger case is deliberately NOT duplicated here: it is
#   hard-BLOCKED by 62-review-dispatch.sh #17 (design/DECISION-DOC.md sec 4, r1/m2). Half
#   A's unique value is the research and wave arms above.
#
# Half B - repeated-manual-friction accumulator (R-6 tripwire, made mechanical). Feeds
#   reviewer `TOOLING:` lines and normalized gating check-command signatures to
#   scripts/instrument-tripwire.py (best-effort); when the SAME check-class recurs >=2
#   times the script fires and the guard relays the message as a warn (build the
#   deterministic check, or record why none is buildable). See instrument-tripwire.py.
#
# The `.deep-research` trigger token is kept DOT-ESCAPED in all prose/messages here
# (rules/20-tool-conventions.md Trigger Escaping); the bare skill-name match is built at
# runtime from fragments so the live command literal never appears in this source.
#
# FAIL-OPEN: jq/python absent, unparseable payload, or a tool outside the watched set ->
# pass silently. State is advisory scratch under $TMPDIR (overridable), never the repo.

GUARD_MODE_WRONG_TOOL=warn

# ── State location (hermetic-test overridable) ────────────────────────────────
# WRONG_TOOL_STATE_DIR overrides the base dir; WRONG_TOOL_SESSION_ID overrides the
# session id (else read from GUARD_STDIN.session_id). Both let the bite-test run
# without touching a real session's scratch.
_wrong_tool_state_dir() {
  local base
  base="${WRONG_TOOL_STATE_DIR:-${TMPDIR:-/tmp}/superclaude-guard-wrong-tool}"
  mkdir -p "$base" 2>/dev/null || return 1
  printf '%s' "$base"
}

_wrong_tool_sid() {
  local sid="${WRONG_TOOL_SESSION_ID:-}"
  if [ -z "$sid" ]; then
    command -v jq >/dev/null 2>&1 \
      && sid=$(printf '%s' "${GUARD_STDIN:-}" | jq -r '.session_id // empty' 2>/dev/null)
  fi
  sid=$(printf '%s' "$sid" | tr -cd 'a-zA-Z0-9_-')
  [ -n "$sid" ] || sid="unknown"
  printf '%s' "$sid"
}

# ── Marker drop (Half A, r1/M3: mechanical, tool_name==Skill) ──────────────────
# Maps a normalized governance-skill name to its marker bucket key. The three
# research-machinery skills (/research, /.deep-research, /wf-websearch) share the
# `research` bucket, so the WebSearch counter's marker check stays a single lookup
# and no live `.deep-research` literal is ever written to a filename.
_wrong_tool_marker_key() {
  local s="$1" dr
  # .deep-research built at runtime from fragments (rules/20 Trigger Escaping) so the
  # contiguous live command literal never appears in this file's source text.
  dr="deep-""research"
  case "$s" in
    research|"$dr"|wf-websearch) printf 'research' ;;
    converge)                    printf 'converge' ;;
    swarm-dispatch)              printf 'swarm-dispatch' ;;
    wf-auto)                     printf 'wf-auto' ;;
    *)                           printf '' ;;
  esac
}

_wrong_tool_marker_drop() {
  [ "${GUARD_TOOL:-}" = "Skill" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0
  local skill key dir sid
  skill=$(guard_field "skill")
  [ -n "$skill" ] || return 0
  skill="${skill#/}"                                   # strip a leading slash
  skill=$(printf '%s' "$skill" | tr '[:upper:]' '[:lower:]')
  key=$(_wrong_tool_marker_key "$skill")
  [ -n "$key" ] || return 0
  dir=$(_wrong_tool_state_dir) || return 0
  sid=$(_wrong_tool_sid)
  : > "$dir/${sid}.marker.${key}" 2>/dev/null || true
}

_wrong_tool_marker_present() {
  local key="$1" dir sid
  dir=$(_wrong_tool_state_dir) || return 1
  sid=$(_wrong_tool_sid)
  [ -f "$dir/${sid}.marker.${key}" ]
}

# ── Half A (i): WebSearch/WebFetch research-saturation counter (PostToolUse) ───
_wrong_tool_websearch_counter() {
  case "${GUARD_TOOL:-}" in WebSearch|WebFetch) ;; *) return 0 ;; esac
  local dir sid cfile wfile count threshold
  dir=$(_wrong_tool_state_dir) || return 0
  sid=$(_wrong_tool_sid)
  cfile="$dir/${sid}.websearch.count"
  wfile="$dir/${sid}.websearch.warned"
  count=$(tr -cd '0-9' <"$cfile" 2>/dev/null); [ -n "$count" ] || count=0
  count=$((count + 1))
  printf '%s' "$count" >"$cfile" 2>/dev/null || return 0
  threshold="${WRONG_TOOL_WEBSEARCH_THRESHOLD:-4}"
  [ "$count" -ge "$threshold" ] || return 0
  _wrong_tool_marker_present research && return 0      # governed machinery engaged; no nag
  [ -f "$wfile" ] && return 0                           # warn once per session
  touch "$wfile" 2>/dev/null || true
  guard_warn "research-saturation shape (>=${threshold} web searches) without governed machinery; use /research or /.deep-research (dot-escaped)"
}

# ── Half A (ii): parallel Agent-batch wave-shape signal (PreToolUse) ───────────
# A >=3-wide batch is detected as >=WIDTH Agent dispatches whose timestamps fall
# inside a short sliding WINDOW (default 3s; the synchronous bite-test widens it).
# One warn per batch: a subsequent batch, a full window later, may warn again.
_wrong_tool_agent_batch() {
  [ "${GUARD_TOOL:-}" = "Agent" ] || return 0
  local dir sid stamps warned now window width recent lastwarn
  dir=$(_wrong_tool_state_dir) || return 0
  sid=$(_wrong_tool_sid)
  stamps="$dir/${sid}.agent.stamps"
  warned="$dir/${sid}.agent.warned"
  now=$(date +%s 2>/dev/null); [ -n "$now" ] || return 0
  window="${WRONG_TOOL_BATCH_WINDOW:-3}"
  width="${WRONG_TOOL_BATCH_WIDTH:-3}"
  printf '%s\n' "$now" >>"$stamps" 2>/dev/null || return 0
  recent=$(awk -v n="$now" -v w="$window" '($0+0) >= (n-w) {c++} END{print c+0}' "$stamps" 2>/dev/null)
  [ -n "$recent" ] || recent=0
  [ "$recent" -ge "$width" ] || return 0
  _wrong_tool_marker_present swarm-dispatch && return 0
  if [ -f "$warned" ]; then
    lastwarn=$(tr -cd '0-9' <"$warned" 2>/dev/null); [ -n "$lastwarn" ] || lastwarn=0
    [ $((now - lastwarn)) -le "$window" ] && return 0
  fi
  printf '%s' "$now" >"$warned" 2>/dev/null || true
  guard_warn "wave/parallel shape (>=${width}-wide Agent batch) without /swarm-dispatch; route a parallel w-* batch through /swarm-dispatch (W-1/W-4/W-7)"
}

# ── Half B: R-6 instrument-tripwire relay (best-effort python) ─────────────────
# Locate scripts/instrument-tripwire.py relative to THIS guard file (guards dir is
# .../staging/hooks/guards; script is .../staging/scripts/...). Overridable for tests.
_wrong_tool_tripwire_script() {
  if [ -n "${WRONG_TOOL_TRIPWIRE_SCRIPT:-}" ]; then
    printf '%s' "$WRONG_TOOL_TRIPWIRE_SCRIPT"; return 0
  fi
  local self dir
  self="${BASH_SOURCE[0]:-}"
  [ -n "$self" ] || { printf ''; return 0; }
  dir=$(cd "$(dirname "$self")" && pwd 2>/dev/null) || { printf ''; return 0; }
  printf '%s' "$dir/../../scripts/instrument-tripwire.py"
}

# Reviewer response text, extracted from GUARD_STDIN.tool_response across the three
# shapes the hook payload can take (string / content-block array / object). Mirrors
# 60-verdict-shape.sh's extraction verbatim (the established idiom). Empty on failure.
_wrong_tool_response_text() {
  [ -n "${GUARD_STDIN:-}" ] || { printf ''; return 0; }
  command -v jq >/dev/null 2>&1 || { printf ''; return 0; }
  printf '%s' "$GUARD_STDIN" | jq -r '
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
  ' 2>/dev/null || printf ''
}

# Feed reviewer TOOLING: lines (Agent responses) to the accumulator; relay any fire.
_wrong_tool_tripwire_tooling() {
  local script="$1" dir="$2" sid="$3" ledger="$4"
  local resp; resp=$(_wrong_tool_response_text)
  [ -n "$resp" ] || return 0
  printf '%s' "$resp" | grep -qi '^[[:space:]]*TOOLING:' || return 0
  local line out
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    out=$(python3 "$script" --session "$sid" --state-dir "$dir" \
            ${ledger:+--ledger "$ledger"} --tooling "$line" 2>/dev/null)
    [ -n "$out" ] && guard_warn "$out"
  done < <(printf '%s\n' "$resp" | grep -i '^[[:space:]]*TOOLING:')
}

# Feed a gating check-command signature to the accumulator; relay any fire. Scoped
# to a governed convergence context (a ledger, or converge machinery engaged this
# session) so this never spawns python on an ordinary Bash call in an ordinary
# session; the script itself is the single classifier of what is a gating command.
_wrong_tool_tripwire_command() {
  local script="$1" dir="$2" sid="$3" ledger="$4"
  [ -n "$ledger" ] || [ -f "$dir/${sid}.marker.converge" ] || return 0
  local cmd out
  cmd=$(guard_command)
  [ -n "$cmd" ] || return 0
  out=$(python3 "$script" --session "$sid" --state-dir "$dir" \
          ${ledger:+--ledger "$ledger"} --command "$cmd" 2>/dev/null)
  [ -n "$out" ] && guard_warn "$out"
}

_wrong_tool_tripwire() {
  command -v python3 >/dev/null 2>&1 || return 0
  local script dir sid ledger
  script=$(_wrong_tool_tripwire_script)
  [ -n "$script" ] && [ -f "$script" ] || return 0
  dir=$(_wrong_tool_state_dir) || return 0
  sid=$(_wrong_tool_sid)
  ledger="${WRONG_TOOL_LEDGER:-}"
  case "${GUARD_TOOL:-}" in
    Agent) _wrong_tool_tripwire_tooling "$script" "$dir" "$sid" "$ledger" ;;
    Bash)  _wrong_tool_tripwire_command "$script" "$dir" "$sid" "$ledger" ;;
  esac
}

# ── Entry points (marker-drop is called in BOTH arms, per the design) ──────────
guard_wrong_tool() {          # PreToolUse arm
  guard_kill_switch && return 0
  _wrong_tool_marker_drop     # Skill -> drop governance marker (before later tools run)
  _wrong_tool_agent_batch     # Agent -> Half A (ii) wave-shape signal
  return 0
}

guardpost_wrong_tool() {      # PostToolUse arm
  guard_kill_switch && return 0
  _wrong_tool_marker_drop        # Skill -> drop governance marker (idempotent)
  _wrong_tool_websearch_counter  # WebSearch/WebFetch -> Half A (i) research-saturation
  _wrong_tool_tripwire           # Half B -> R-6 instrument tripwire
  return 0
}
