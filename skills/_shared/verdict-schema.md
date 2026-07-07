# _shared/verdict-schema.md: two-token verdict protocol (SOT)

Consumed by: /converge, /review-dispatch, w-hostile-reviewer, w-reviewer, w-design-reviewer, /sanity-check, wf-* drivers, /goal seal strings, dispatch-contract.md.

## Tokens (always LINE 1 of the agent's final message)

1. Working review (every round):
   `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K`
2. Final acceptance audit (fresh holistic auditor only; never the round reviewer):
   `SEAL: ACCEPTED|REJECTED blocking=N major=N minor=N nits=N`
3. Non-reviewer worker return:
   `STATUS: DONE|PARTIAL|FAILED files=N checkpoint=<path>`

## Rules

- **Verdict-first**: the token line is line 1. Result truncation cuts tails; the token must survive.
- **Provenance**: only reviewer subagents emit VERDICT/SEAL. Producers and conductors never author them. The conductor QUOTES reviewer token lines verbatim into the transcript; the /goal evaluator reads only the conversation and accepts only conductor-quoted reviewer tokens.
- **Dual-condition exit**: `SEAL: ACCEPTED` terminates a loop only together with the producer's separate completion statement (two independent signals).
- **Evidence bar**: findings without a file:line citation (or equivalent: DOI/arxiv ID, re-run expected-vs-actual, named principle + clause) are dropped before counting. A zero-finding review is a valid outcome.
- **Anti-hacking sweep** (automatic `blocking`, any scope): test-file edits that special-case, weakened assertions, harness escapes (e.g. forced exit-0), skipped/deleted coverage, diagnostic theater in infra tests or health scripts.
- **Nit policy**: after round 1, NEW minor findings are logged but do not gate convergence, unless the loop runs `--strict`. Minor counts still appear in the token line.
- **Post-compaction requote**: before continuing a loop after compaction, the conductor re-quotes the latest token line from the round ledger.

## Severity normalization (legacy vocabularies map to blocking/major/minor)

| Source vocabulary | blocking | major | minor |
|---|---|---|---|
| hostile-review Blocking/Major/Minor (PASS = clean) | Blocking | Major | Minor |
| w-reviewer REJECT/CONDITIONAL/PASS | REJECT | CONDITIONAL | polish notes |
| design-review Blocker/High/Medium/Nit | Blocker | High | Medium, Nit |
| sanity-check BLOCK_MERGE/NEEDS_FIXES/CLEAN | BLOCK_MERGE | NEEDS_FIXES | notes |
| deterministic checkers (figure-validate FLAG, test-infra FAIL, score gates) | failed gate | n/a | n/a |
