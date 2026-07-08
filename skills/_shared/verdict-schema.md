# _shared/verdict-schema.md: two-token verdict protocol (SOT)

Consumed by: /converge, /review-dispatch, w-hostile-reviewer, w-reviewer, w-design-reviewer, /sanity-check, wf-* drivers, /goal condition strings, dispatch-contract.md.

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
- **Nit policy**: governed by the bar level below. At the default bar, after round 1 NEW minor findings are logged but do not gate; at the gate and strict bars, minors and nits gate. Minor counts always appear in the token line.
- **No pre-approval (no rubber-stamping)**: every VERDICT and every SEAL must derive from a fresh, explicit examination of the CURRENT state of the work, with evidence gathered THIS round. Approval never transfers across rounds: a reviewer may not approve round N+1 on the basis of round N, may not anticipate a future clean state, and may not rest a verdict on "my prior findings were addressed" without re-examining the current artefact and citing fresh file:line evidence. A SEAL is bound to a specific artefact revision (name the commit hash or round); ANY change to the artefact after a SEAL voids it and requires a fresh SEAL. The conductor never quotes a stale token: the goal evaluator may act only on a SEAL the conductor states is the MOST RECENT reviewer return AND post-dates the last change to the artefact. The producer's completion statement is a signal, not an approval; the independent SEAL is the only approval, and it must come from a fresh auditor that examined the COMPLETE final state, not a delta.
- **Post-compaction requote**: before continuing a loop after compaction, the conductor re-quotes the latest token line from the round ledger.

## Bar levels (SOT)

The convergence bar has three tiers (a wf-* binding or `/converge` selects one):

| Bar | Selected by | Seal requires | Notes |
|---|---|---|---|
| default | (nothing) | `blocking=0 major=0` | minors + nits logged; do not gate after round 1 |
| gate | `--stakes gate`, or a gate campaign | `blocking=0 major=0 minor=0 nits=0` | everything gates; single clean SEAL |
| strict | `--strict` | the gate bar AND two consecutive clean SEALs | submission-grade (DEC-R1); the extra tightening over gate is the second consecutive clean audit, each a fresh examination (no inherited approval) |

`strict` is a superset of `gate`, which is a superset of `default`. nits=0 belongs to `gate`; the "two consecutive" requirement is what `strict` adds. A gate campaign uses the `gate` bar unless it also passes `--strict`.

## Canonical emitted /goal block (SOT)

`/goal` takes a natural-language CONDITION (not a pseudo-command). Every wf-* binding emits this shape, specialising the bracketed slots; it never invents a `/goal seal ...` subcommand:

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH <reviewer> return, is the MOST RECENT such line, and post-dates the last change to <artefact>, reporting blocking=0 major=0 <minor=0 nits=0 at gate/strict>; (2) the producer has separately stated completion (STATUS: DONE); (3) <workflow-specific extra, or omit>. If review rounds exceed <cap>, or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Producer-completion (clause 2) and workflow-specific slot (3) by binding: wf-design = clause 2 `STATUS: DONE` from the w-doc producer, slot 3 omitted; wf-report = clause 2 `STATUS: DONE`, slot 3 = "the latest revision compiles clean (latexmk, zero material warnings)"; wf-websearch = clause 2 IS saturation ("a wave surfaced zero new must-read sources"), since a search loop has no separate STATUS: DONE, slot 3 omitted.

## Severity normalization (legacy vocabularies map to blocking/major/minor)

| Source vocabulary | blocking | major | minor |
|---|---|---|---|
| hostile-review Blocking/Major/Minor (PASS = clean) | Blocking | Major | Minor |
| w-reviewer REJECT/CONDITIONAL/PASS | REJECT | CONDITIONAL | polish notes |
| design-review Blocker/High/Medium/Nit | Blocker | High | Medium, Nit |
| sanity-check BLOCK_MERGE/NEEDS_FIXES/CLEAN | BLOCK_MERGE | NEEDS_FIXES | notes |
| deterministic checkers (figure-validate FLAG, test-infra FAIL, score gates) | failed gate | n/a | n/a |
