# ~/.claude/hooks/guards/lib-guard.sh — guard subsystem FOUNDATION (PHASE2-CONTRACT sec 3).
#
# Defines functions ONLY at source time; no top-level side effects (sourcing is inert).
# Provides, after guard_init (called once by the dispatcher with the raw hook stdin JSON):
#   GUARD_TOOL        tool name           GUARD_AGENT   resolved agent (walk_to_agent,
#   GUARD_INPUT_JSON  raw .tool_input                   lib.sh; else the per-session
#   GUARD_STDIN       raw hook stdin JSON               agent marker; "" = unresolved)
# Accessors (read the parsed context, never re-parse stdin):
#   guard_field <key|dotted.path>  guard_file_path  guard_command
#   guard_new_content (Write/Edit/MultiEdit new text)  guard_agent_prompt (Agent prompt)
# Mode + emit:
#   guard_mode <NAME> [default]  guard_kill_switch  guard_block "msg"  guard_warn "msg"
# Harness:
#   run_guard <fn>  (calls fn only if declare -F says it exists; sets GUARD_CURRENT_NAME)
#
# SAFETY: MUST FAIL-OPEN. Any internal/parse error prints a WARN to stderr and passes
# (return 0); a guard NEVER bricks a tool call on an internal fault. Only an explicit
# guard_block in block mode blocks (exit 2). Prefer jq; degrade if jq or a field is absent.

# ── guard_init: parse the hook stdin once into GUARD_* context ────────────────
# Arg: $1 = raw hook stdin JSON (defaults to $INPUT set by the dispatcher).
guard_init() {
  local raw="${1:-${INPUT:-}}"
  GUARD_STDIN="$raw"
  GUARD_TOOL=""
  GUARD_INPUT_JSON="{}"
  GUARD_AGENT=""
  GUARD_PARSE_OK=1

  if ! command -v jq >/dev/null 2>&1; then
    printf 'GUARD-WARN [lib]: jq unavailable; guards fail-open (passing all)\n' >&2
    GUARD_PARSE_OK=0
    return 0
  fi
  if [ -z "$raw" ]; then
    printf 'GUARD-WARN [lib]: empty hook stdin; guards fail-open\n' >&2
    GUARD_PARSE_OK=0
    return 0
  fi

  GUARD_TOOL=$(printf '%s' "$raw" | jq -r '.tool_name // ""' 2>/dev/null) \
    || { GUARD_TOOL=""; GUARD_PARSE_OK=0; }
  GUARD_INPUT_JSON=$(printf '%s' "$raw" | jq -c '.tool_input // {}' 2>/dev/null) \
    || { GUARD_INPUT_JSON="{}"; GUARD_PARSE_OK=0; }

  # Resolve the agent via the shared proc-tree walk (hooks/lib.sh). Absent => "".
  if declare -F walk_to_agent >/dev/null 2>&1; then
    GUARD_AGENT=$(walk_to_agent 2>/dev/null || echo "")
  fi

  # Fallback: the proc-tree walk yields "" whenever the hook runs detached from the
  # claude process (its parent is PID 1 at once, so the walk has nothing to climb),
  # which would leave every identity-gated guard stuck at its default-deny and block
  # the sanctioned writer. Fall back to the per-session agent marker written by
  # hooks/modules/00-parse.sh: the identity SOT already read by session-cleanup.sh,
  # agent-outcome.sh, pre-compact.sh and the session scripts. The session id is
  # sanitized exactly as 00-parse.sh does, so it can never traverse out of the timer
  # dir. 00-parse.sh caches an EMPTY marker for a session started with no --agent, so
  # only a NON-EMPTY marker resolves; an empty one stays unresolved and keeps the
  # default-deny. Fail-open: a missing or unreadable marker just leaves GUARD_AGENT "".
  if [ -z "$GUARD_AGENT" ]; then
    local sid tdir amark
    sid=$(printf '%s' "$raw" | jq -r '.session_id // ""' 2>/dev/null) || sid=""
    [ -n "$sid" ] || sid="${CLAUDE_CODE_SESSION_ID:-}"
    sid=$(printf '%s' "$sid" | tr -cd 'a-zA-Z0-9_-')
    tdir="${SUPERCLAUDE_SESSION_TIMER_DIR:-$HOME/.claude/session-timers}"
    if [ -n "$sid" ] && [ -f "$tdir/$sid.agent" ]; then
      amark=$(tr -d '[:space:]' <"$tdir/$sid.agent" 2>/dev/null) || amark=""
      [ -n "$amark" ] && GUARD_AGENT="$amark"
    fi
  fi

  if [ "$GUARD_PARSE_OK" -ne 1 ]; then
    printf 'GUARD-WARN [lib]: hook stdin JSON parse degraded; affected guards fail-open\n' >&2
  fi
  return 0
}

# ── Field accessors (operate on GUARD_INPUT_JSON = the .tool_input object) ─────
# guard_field <key|dotted.path>: extract a tool_input field as a string. Missing => "".
guard_field() {
  local key="${1:-}"
  [ -n "$key" ] || { echo ""; return 0; }
  [ -n "${GUARD_INPUT_JSON:-}" ] || { echo ""; return 0; }
  command -v jq >/dev/null 2>&1 || { echo ""; return 0; }
  printf '%s' "$GUARD_INPUT_JSON" | jq -r --arg k "$key" '
    (try getpath($k | split(".")) catch null) as $v
    | if   $v == null           then ""
      elif ($v | type) == "string" then $v
      else ($v | tostring) end
  ' 2>/dev/null || echo ""
}

guard_file_path()   { guard_field "file_path"; }
guard_command()     { guard_field "command"; }
guard_agent_prompt(){ guard_field "prompt"; }

# guard_new_content: the newly written/edited text, diff-scoped.
#   Write     -> .content
#   Edit      -> .new_string
#   MultiEdit -> each .edits[].new_string
# Each non-empty piece is emitted on its own line (empty output if none).
guard_new_content() {
  [ -n "${GUARD_INPUT_JSON:-}" ] || { echo ""; return 0; }
  command -v jq >/dev/null 2>&1 || { echo ""; return 0; }
  printf '%s' "$GUARD_INPUT_JSON" | jq -r '
    [ (.content    // empty),
      (.new_string // empty),
      ((.edits // []) | .[]? | (.new_string // empty)) ]
    | .[]
  ' 2>/dev/null || echo ""
}

# ── Mode + kill-switch (two-stage rollout, PHASE2-CONTRACT sec 4) ─────────────
# guard_kill_switch: returns 0 (true) when the global kill-switch is engaged.
guard_kill_switch() {
  [ "${SUPERCLAUDE_GUARDS:-}" = "off" ]
}

# guard_mode <NAME> [default]: resolve off|warn|block.
#   1. global kill-switch SUPERCLAUDE_GUARDS=off  -> off
#   2. per-guard env SUPERCLAUDE_GUARD_<NAME>     -> that value (if valid)
#   3. passed default (fallback block)
guard_mode() {
  local name def val envvar
  name=$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')
  def="${2:-block}"
  if [ "${SUPERCLAUDE_GUARDS:-}" = "off" ]; then echo "off"; return 0; fi
  envvar="SUPERCLAUDE_GUARD_${name}"
  val="${!envvar:-}"
  case "$val" in
    off|warn|block) echo "$val"; return 0 ;;
  esac
  case "$def" in
    off|warn|block) echo "$def"; return 0 ;;
    *)              echo "block"; return 0 ;;
  esac
}

# ── Emit helpers (mode-aware) ─────────────────────────────────────────────────
# guard_block "msg": in block mode print a BLOCK reason to stderr and exit 2; in
# warn mode degrade to a WARN and return 0; in off mode return 0 silently. The
# effective mode is resolved from the CURRENT guard (GUARD_CURRENT_NAME set by
# run_guard) and its declared default GUARD_MODE_<NAME>. In a PostToolUse phase
# (GUARD_PHASE=post) a block always degrades to a warn: a completed tool cannot
# be blocked.
guard_block() {
  local msg="${1:-guard violation}"
  local name="${GUARD_CURRENT_NAME:-UNKNOWN}"
  local defvar="GUARD_MODE_${name}"
  local def="${!defvar:-block}"
  local mode
  mode=$(guard_mode "$name" "$def")
  if [ "${GUARD_PHASE:-pre}" = "post" ] && [ "$mode" = "block" ]; then
    mode="warn"
  fi
  case "$mode" in
    off)  return 0 ;;
    warn) printf 'GUARD-WARN [%s]: %s (degraded from block by mode)\n' "$name" "$msg" >&2; return 0 ;;
    *)    printf 'GUARD-BLOCK [%s]: %s\n' "$name" "$msg" >&2; exit 2 ;;
  esac
}

# guard_warn "msg": always a non-blocking WARN to stderr, return 0. The global
# kill-switch silences it (true no-op when SUPERCLAUDE_GUARDS=off).
guard_warn() {
  local msg="${1:-guard warning}"
  local name="${GUARD_CURRENT_NAME:-UNKNOWN}"
  [ "${SUPERCLAUDE_GUARDS:-}" = "off" ] && return 0
  printf 'GUARD-WARN [%s]: %s\n' "$name" "$msg" >&2
  return 0
}

# ── Harness ───────────────────────────────────────────────────────────────────
# run_guard <fn>: invoke the guard entry function ONLY if it is defined. Derives
# the guard NAME (for mode resolution) from the function name by stripping the
# guard_/guardpost_ prefix, uppercasing, and mapping '-' to '_'.
run_guard() {
  local fn="${1:-}"
  [ -n "$fn" ] || return 0
  declare -F "$fn" >/dev/null 2>&1 || return 0
  local name="${fn#guardpost_}"
  name="${name#guard_}"
  GUARD_CURRENT_NAME=$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')
  "$fn"
}
