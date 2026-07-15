# Guard: 62-review-dispatch: F5 #17/#20, round-ledger + reviewer-isolation
# (PHASE2-CONTRACT sec 8 reviewer set, design/enforcement-gap-ledger.md Family 5
# #17/#20). Fires only on GUARD_TOOL==Agent where .tool_input.subagent_type is
# one of the reviewer set {w-reviewer, w-hostile-reviewer, w-design-reviewer}
# (PHASE2-CONTRACT sec 8; matches converge/SKILL.md's round-reviewer roster).
#
# Checks:
#   #17 LEDGER-EXISTS (BLOCK, scoped to a converge context — SEAL-A M3 fix): a
#       reviewer dispatch prompt must carry a `Ledger: <path>` line (mirrors the
#       existing `Checkpoint: <path>` line in dispatch-contract.md section 6)
#       AND that path must already exist on disk. This mechanises the rejected
#       "Conductor pre-flight" prose (converge/SKILL.md's round-ledger step)
#       that previously relied on the conductor remembering to create
#       rounds.md before round 1 (verdict-schema.md No pre-approval: every
#       VERDICT derives from a ledgered round, never an unrecorded one).
#       The ledger requirement only makes sense INSIDE a /converge round; an
#       ad-hoc w-hostile-reviewer run or a SEAL panel is a legitimate reviewer
#       dispatch with no ledger and must not be blocked (SEAL-A verdict M3:
#       the original unscoped block would have bricked the very SEAL panel
#       that found the bug). Scoped via _guard_review_dispatch_in_converge
#       below — see that function for the detection mechanism.
#   #20 ISOLATION-LINT (WARN): scans the prompt for phrases that indicate
#       producer context leaked into a reviewer dispatch (dispatch-contract.md
#       section 7 "Reviewer isolation": artifact + diff + rubric ONLY, never
#       the producer's reasoning, self-assessment, or prior clean verdicts).
#       This is a LEXICAL heuristic, not semantic: it can both false-positive
#       (an artifact that legitimately quotes one of these phrases, e.g. a
#       rubric instructing the reviewer to check for self-assessment leakage)
#       and false-negative (paraphrased leakage the wordlist misses). WARN
#       only, never BLOCK, for that reason (enforcement-gap-ledger.md Family 5
#       #20 "PARTIAL/heuristic").
#
# FAIL-OPEN: subagent_type not in the reviewer set, or the tool_input cannot
# be parsed (GUARD_TOOL resolves empty) -> pass. lib-guard's own parse
# degradation (guard_init) already covers the latter.

GUARD_MODE_REVIEW_DISPATCH=block

# ── Converge-context detection (SEAL-A M3 fix) ───────────────────────────────
# #17 is only meaningful inside a /converge round. Detect that context via the
# SAME per-session marker 70-wrong-tool.sh drops when the `converge` governance
# skill is invoked (_wrong_tool_marker_drop, bucket key "converge") — checked
# through _wrong_tool_marker_present, not a re-implementation, so the marker
# path convention (state dir + session id resolution, both env-overridable for
# tests) has one source of truth. guard-dispatch.sh's guards/[0-9]*.sh glob
# sources every guard file (function definitions only) before any run_guard
# call runs, so by the time guard_review_dispatch executes, 70-wrong-tool.sh's
# functions are already defined regardless of the two files' numeric order.
# Fail OPEN (no converge context assumed) if the marker functions are not
# defined at all — e.g. an isolated harness sourcing only this guard — matching
# the guard subsystem's fail-open contract (lib-guard.sh header).
_guard_review_dispatch_in_converge() {
  declare -F _wrong_tool_marker_present >/dev/null 2>&1 || return 1
  _wrong_tool_marker_present converge
}

# ── #17: Ledger: <path> line present, and the path exists ───────────────────
# Accepts the line bare (`Ledger: <path>`) or bullet-prefixed (`- Ledger:
# <path>` / `* Ledger: <path>`), optionally indented, mirroring how
# dispatch-contract's Checkpoint: line is written in practice. Scoped to a
# converge context (see _guard_review_dispatch_in_converge): outside one, an
# ad-hoc reviewer dispatch or a SEAL panel passes without a ledger.
_guard_review_dispatch_ledger() {
  local prompt="$1" path
  _guard_review_dispatch_in_converge || return 0
  path=$(printf '%s' "$prompt" \
    | grep -oP -- '(^|[-*])[[:space:]]*Ledger:[[:space:]]*\K\S+' | head -1)
  if [ -z "$path" ] || [ ! -f "$path" ]; then
    guard_block "reviewer dispatch without a round ledger; create <campaign>/rounds.md and pass a 'Ledger: <path>' line before the first review (converge pre-flight #1, verdict-schema R-5)"
  fi
}

# ── #20: producer-context leakage wordlist (lexical, WARN only) ─────────────
_guard_review_dispatch_isolation() {
  local prompt="$1" hit
  hit=$(printf '%s' "$prompt" | grep -oiP -- \
    'producer-[^[:space:]]*\.md|checkpoints/producer[^[:space:]]*|prior verdict|prior finding|round [0-9]+ claims|the producer says|self-assessment|already fixed|previously found' \
    | head -1)
  [ -n "$hit" ] || return 0
  guard_warn "possible reviewer-isolation violation: '$hit' in the dispatch prompt; a reviewer gets artifact+diff+rubric ONLY (dispatch-contract, verdict-schema No pre-approval)"
}

guard_review_dispatch() {
  [ "${GUARD_TOOL:-}" = "Agent" ] || return 0

  local subtype
  subtype=$(guard_field "subagent_type")
  case "$subtype" in
    w-reviewer|w-hostile-reviewer|w-design-reviewer) ;;
    *) return 0 ;;
  esac

  local prompt
  prompt=$(guard_agent_prompt)

  _guard_review_dispatch_ledger "$prompt"
  _guard_review_dispatch_isolation "$prompt"
  return 0
}
