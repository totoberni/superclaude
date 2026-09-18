# _shared/verdict-schema.md: two-token verdict protocol (SOT)

Consumed by: w-hostile-reviewer, w-reviewer, w-design-reviewer, /sanity-check, dispatch-contract.md.

## Tokens (always LINE 1 of the agent's final message)

1. Reviewer return:
   `VERDICT: REWORK|CLEAN blocking=N major=N minor=N`
2. Producer (non-reviewer worker) return:
   `STATUS: DONE|PARTIAL|FAILED files=N checkpoint=<path>`

## Rules

- **Verdict-first**: the token line is line 1. Result truncation cuts tails; the token must survive.
- **Provenance**: only reviewer subagents emit VERDICT. Producers never author it; they emit STATUS. A parent that relays a reviewer's result QUOTES the token line verbatim.
- **Single-pass**: a review is one dispatch and one verdict. The parent reads the verdict and fixes the findings; a re-review happens only if the owner or meta judges it necessary. There is no mandated iteration protocol.
- **Evidence bar**: findings without a file:line citation (or equivalent: DOI/arxiv ID, re-run expected-vs-actual, named principle + clause) are dropped before counting. A zero-finding review is a valid outcome.
- **Anti-hacking sweep** (automatic `blocking`, any scope): test-file edits that special-case, weakened assertions, harness escapes (e.g. forced exit-0), skipped/deleted coverage, diagnostic theater in infra tests or health scripts.
- **CLEAN vs REWORK**: `CLEAN` requires `blocking=0 major=0`. Minor findings are logged and do not gate; minor counts always appear in the token line.
- **No pre-approval (no rubber-stamping)**: every VERDICT must derive from a fresh, explicit examination of the CURRENT state of the work, with evidence gathered in THIS review. Approval never transfers: a re-review may not rest on "my prior findings were addressed" without re-examining the current artefact and citing fresh file:line evidence. The producer's `STATUS: DONE` is a signal, not an approval.

## Severity normalization (legacy vocabularies map to blocking/major/minor)

| Source vocabulary | blocking | major | minor |
|---|---|---|---|
| hostile-review Blocking/Major/Minor (PASS = clean) | Blocking | Major | Minor |
| w-reviewer REJECT/CONDITIONAL/PASS | REJECT | CONDITIONAL | polish notes |
| design-review Blocker/High/Medium/Nit | Blocker | High | Medium, Nit |
| sanity-check BLOCK_MERGE/NEEDS_FIXES/CLEAN | BLOCK_MERGE | NEEDS_FIXES | notes |
| deterministic checkers (figure-validate FLAG, score gates) | failed gate | n/a | n/a |
