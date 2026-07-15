# Guard: 60-verdict-shape (PostToolUse on Agent). Family 5 governance-protocol
# validator, invariants #18/#19 (design/enforcement-gap-ledger.md). Extracts the
# canonical VERDICT/SEAL/STATUS grammar from ~/.claude/scripts/swarm/converge_auto.py
# (VERDICT_RE/SEAL_RE/PRODUCER_TOKEN_RE, lines ~105-122) and the role sets from
# design/PHASE2-CONTRACT.md sec 8. Never blocks (PostToolUse cannot); every finding
# here is a guard_warn. FAIL-OPEN: an unresolvable subagent_type, an unparseable
# payload, or a subagent outside both role sets returns silently.
#
# Role sets (PHASE2-CONTRACT sec 8):
#   producer = {w-implementer, w-doc, w-refactorer, w-merger}
#   reviewer = {w-reviewer, w-hostile-reviewer, w-design-reviewer}
#
# Checks:
#   #18 TOKEN SHAPE  : a reviewer's first non-blank output line looks like an
#                       attempted verdict token (an uppercase word immediately
#                       followed by a colon) but does not match the canonical
#                       `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=N`
#                       or `SEAL: ACCEPTED|REJECTED blocking=N major=N minor=N
#                       nits=N` grammar (verdict-schema.md § Tokens). Catches
#                       invented vocabulary (e.g. `ACCEPT:`, `PASS:`) and malformed
#                       VERDICT:/SEAL: lines alike.
#   #19 PROVENANCE   : a producer's output contains ANY line matching
#                       `^\s*(VERDICT|SEAL):` anywhere (not just line 1); producers
#                       are STATUS-only (verdict-schema.md § Provenance). Mirrors
#                       converge_auto.py's PRODUCER_TOKEN_RE.

GUARD_MODE_VERDICT_SHAPE=warn

# _guard_verdict_shape_role <subagent_type>: echo "producer" | "reviewer" | "".
_guard_verdict_shape_role() {
  case "$1" in
    w-implementer|w-doc|w-refactorer|w-merger) echo "producer" ;;
    w-reviewer|w-hostile-reviewer|w-design-reviewer) echo "reviewer" ;;
    *) echo "" ;;
  esac
}

# _guard_verdict_shape_response_text: the subagent's returned text, extracted from
# GUARD_STDIN's .tool_response. Mirrors agent-outcome.sh's extraction verbatim (that
# hook is the existing PostToolUse-on-Agent consumer of this exact field) so this
# guard reads the same three tool_response shapes (string / content-block array /
# object) the real hook payload can take. Empty on any parse failure (fail-open).
_guard_verdict_shape_response_text() {
  [ -n "${GUARD_STDIN:-}" ] || { echo ""; return 0; }
  command -v jq >/dev/null 2>&1 || { echo ""; return 0; }
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
  ' 2>/dev/null || echo ""
}

# #18: reviewer's first non-blank line must be a canonical VERDICT:/SEAL: line.
_guard_verdict_shape_check_reviewer() {
  local resp="$1" first_line verdict_re seal_re tokenish_re
  first_line=$(printf '%s\n' "$resp" | grep -m1 -v '^[[:space:]]*$')
  [ -n "$first_line" ] || return 0
  first_line="${first_line#"${first_line%%[![:space:]]*}"}"
  first_line="${first_line%"${first_line##*[![:space:]]}"}"

  # Canonical grammar (verdict-schema.md § Tokens; mirrors converge_auto.py
  # VERDICT_RE/SEAL_RE verbatim).
  verdict_re='^VERDICT: (REWORK|CLEAN) blocking=[0-9]+ major=[0-9]+ minor=[0-9]+ round=[0-9]+$'
  seal_re='^SEAL: (ACCEPTED|REJECTED) blocking=[0-9]+ major=[0-9]+ minor=[0-9]+ nits=[0-9]+$'
  [[ "$first_line" =~ $verdict_re ]] && return 0
  [[ "$first_line" =~ $seal_re ]] && return 0

  # "Verdict-like": an uppercase word immediately followed by a colon; the shape
  # every legal token line has. Only these get flagged; ordinary prose (no line-1
  # token attempt at all) is not a shape violation this guard can safely judge.
  tokenish_re='^[A-Z][A-Za-z0-9_]*:'
  if [[ "$first_line" =~ $tokenish_re ]]; then
    guard_warn "reviewer emitted an unrecognized verdict token; expected VERDICT:/SEAL: per verdict-schema.md"
  fi
  return 0
}

# #19: producer output must never carry a VERDICT:/SEAL: line anywhere.
_guard_verdict_shape_check_producer() {
  local resp="$1"
  printf '%s\n' "$resp" | grep -Eq '^[[:space:]]*(VERDICT|SEAL):' || return 0
  guard_warn "producer subagent emitted a VERDICT/SEAL token; producers are STATUS-only (verdict-schema.md Provenance)"
}

guardpost_verdict_shape() {
  guard_kill_switch && return 0
  [ "${GUARD_TOOL:-}" = "Agent" ] || return 0

  local subagent_type role resp
  subagent_type=$(guard_field "subagent_type")
  [ -n "$subagent_type" ] || return 0

  role=$(_guard_verdict_shape_role "$subagent_type")
  [ -n "$role" ] || return 0

  resp=$(_guard_verdict_shape_response_text)
  [ -n "$resp" ] || return 0

  case "$role" in
    reviewer) _guard_verdict_shape_check_reviewer "$resp" ;;
    producer) _guard_verdict_shape_check_producer "$resp" ;;
  esac
  return 0
}
