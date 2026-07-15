# Guard: 62-review-dispatch: F5 #17/#20, round-ledger + reviewer-isolation
# (PHASE2-CONTRACT sec 8 reviewer set, design/enforcement-gap-ledger.md Family 5
# #17/#20). Fires only on GUARD_TOOL==Agent where .tool_input.subagent_type is
# one of the reviewer set {w-reviewer, w-hostile-reviewer, w-design-reviewer}
# (PHASE2-CONTRACT sec 8; matches converge/SKILL.md's round-reviewer roster).
#
# Checks:
#   #17 LEDGER-EXISTS (BLOCK): a reviewer dispatch prompt must carry a
#       `Ledger: <path>` line (mirrors the existing `Checkpoint: <path>` line
#       in dispatch-contract.md section 6) AND that path must already exist on
#       disk. This mechanises the rejected "Conductor pre-flight" prose
#       (converge/SKILL.md's round-ledger step) that previously relied on the
#       conductor remembering to create rounds.md before round 1
#       (verdict-schema.md No pre-approval: every VERDICT derives from a
#       ledgered round, never an unrecorded one).
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

# ── #17: Ledger: <path> line present, and the path exists ───────────────────
# Accepts the line bare (`Ledger: <path>`) or bullet-prefixed (`- Ledger:
# <path>` / `* Ledger: <path>`), optionally indented, mirroring how
# dispatch-contract's Checkpoint: line is written in practice.
_guard_review_dispatch_ledger() {
  local prompt="$1" path
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
