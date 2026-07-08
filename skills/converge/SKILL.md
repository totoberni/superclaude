---
name: converge
description: "Use when converging an artefact through produce-review rounds to a seal"
category: delegation
user-invocable: true
argument-hint: "<target> [--rounds N] [--strict] [--panel] [--binding B1|B2|B3|B4|B5]"
allowed-tools: Read, Write, Edit, Bash, Agent, Skill
---

# Converge

Generic convergence-loop driver: iterate produce, then review, until a fresh auditor seals the artifact. It operationalises the two-token verdict protocol (`_shared/verdict-schema.md`) over the `/goal` engine. Converge configures and runs the rounds; the printed `/goal` block lets an independent engine enforce the exit conditions. The skill never arms the engine itself.

**Authority**: meta + orch only. Workers (`w-*`) cannot spawn reviewers, so invoking converge from a worker is a no-op error. The conductor (meta or orch) owns the loop, quotes verdicts, and maintains the ledger.

**Conductor context (tool convention for bound skills)**: loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the CONDUCTOR's context, which holds Agent + Skill. A skill bound into a converge loop declares in its own `allowed-tools` only what its SINGLE invocation needs; its loop-integration section is documented as conductor-driven, not self-driven. A skill that forks to a worker (`agent:` / `context: fork`) is therefore a round REVIEWER or producer, never the loop driver, and never seals itself: the terminal SEAL always comes from a fresh auditor of a DIFFERENT identity than any round reviewer (verdict-schema.md, No pre-approval + Provenance).

## Iteration protocol (one round)

Each round runs five steps in order.

1. **PRODUCE / REVISE**: delegate the build (round 1) or the punch-list fixes (later rounds) to a producer worker per `dispatch-contract.md`. The model split applies: expected <=20 tool calls keeps sonnet-class defaults; beyond ~20-24 the dispatch overrides to `model: opus` regardless of worker class (`dispatch-contract.md` section 5). Producers never self-certify.
2. **PERSIST**: the producer writes the artifact to disk, then the conductor appends a ledger entry (round, delta, open-findings count) before any review runs. Checkpoint-first: load-bearing content lives on disk, not in the final message.
3. **REVIEW**: resolve the reviewer through `/review-dispatch` (artifact class to w- type + rubric + effort). Round 1 uses a FRESH reviewer; middle rounds reuse a delta-scoped reviewer, rehydrated from the ledger (persistent-via-respawn is the DEFAULT: SendMessage continuation is gated behind the agent-teams experiment and not assumed available); the final seal audit is always a FRESH holistic auditor examining the COMPLETE current state. Every reviewer receives artifact + diff + rubric ONLY, re-examines the CURRENT state and cites fresh evidence THIS round, and emits a VERDICT line (the seal auditor emits SEAL). No pre-approval: a reviewer never approves a round on the strength of a prior round, and any change after a SEAL voids it (verdict-schema.md, No pre-approval).
4. **REPORT**: the conductor quotes the reviewer's VERDICT line verbatim into the transcript and appends it to the ledger. Only reviewers author tokens; the conductor relays them.
5. **TRIAGE**: accept or contest each finding with evidence (file:line, re-run expected-vs-actual, or a named principle and clause). Accepted findings become the next round's punch list; contested ones are logged with a rebuttal.

## The 8 loop rules

a. **Rubric-bound reviewers.** Every review dispatch carries an explicit rubric; never "review this". The rubric is preloaded per `/review-dispatch`.
b. **Reviewer isolation.** The reviewer sees artifact + diff + rubric only. Never pass the producer's reasoning, self-assessment, or prior clean verdicts (`dispatch-contract.md` section 7).
c. **Tool-verified critique.** Reviewers RUN the tests, linters, and compile gates themselves. A producer's self-report is never the evidence of record.
d. **Anti-hacking sweep.** Automatic blocking finding, any scope: test special-casing, weakened assertions, harness escapes (forced exit-0), skipped or deleted coverage, diagnostic theatre in infra tests or health scripts. This sweep runs every round without being asked.
e. **Two-token protocol.** The reviewer emits a per-round VERDICT line; termination comes only from a SEAL line emitted by the final fresh auditor. Producers are contractually forbidden from emitting either token. The only two token line formats (SOT: `verdict-schema.md`):
   - `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K`
   - `SEAL: ACCEPTED|REJECTED blocking=N major=N minor=N nits=N`
f. **Dual-condition exit.** A clean `SEAL: ACCEPTED` terminates the loop only together with the producer's separate completion statement (its `STATUS: DONE`), two independent signals.
g. **Nit policy / bar levels.** The seal bar has three tiers (verdict-schema.md, Bar levels): default (`blocking=0 major=0`; minors + nits logged, do not gate after round 1), gate (`--stakes gate` or a gate campaign: `blocking=0 major=0 minor=0 nits=0`), strict (`--strict`: the gate bar AND two consecutive clean SEALs). Minor counts always appear in every token line.
h. **Caps.** Default `--rounds 4`. Escalate if total findings do not decrease for 2 consecutive rounds (stall or oscillation). `--panel` (2-3 diverse-tier judges, at least one opus, majority vote) is reserved for irreversible gates (DEC-R5).

## Engine bindings (B1-B5)

`--binding` selects the loop's driving cadence. Expensive adversarial review fires per completion, never per tick.

| Binding | When to use | Cadence |
|---|---|---|
| B1 goal-sealed convergence | Default. One artifact iterated to a clean seal. | Event: each producer or reviewer return advances the loop; the goal seal terminates. |
| B2 phased campaign | Multi-phase build with handoffs between fresh sessions. | Cron, or 30-60m phases; each phase a fresh session rehydrated from a handoff file. |
| B3 poll-then-act monitor | Watch an external signal, act when it changes. | Fixed 10-15m poll, or a dynamic `/loop` tick gated by a goal seal. |
| B4 gardener / maintenance | Ongoing upkeep with no fixed endpoint. | Dynamic bare `/loop`; a hot-editable `loop.md` steers it live. |
| B5 watchdog | Supervise another loop's health. | Short fixed `/loop` reading the round-ledger heartbeat; escalates on stall or oscillation. |

## Round ledger

- **Path**: `<campaign plans dir>/rounds.md` by default, or alongside the artifact for a standalone loop.
- **Entry fields**: timestamp, round number, delta (files or scope this round), the reviewer VERDICT line quoted verbatim, open-findings count.
- **Heartbeat**: a B5 watchdog reads the latest entry's timestamp and findings trend as a liveness signal; a stalled or oscillating ledger triggers escalation.
- **Post-compaction rule**: before continuing a loop after compaction, the conductor re-quotes the latest VERDICT line from the ledger into the transcript (`verdict-schema.md`, post-compaction requote).

## Goal-string emission (DEC-R2)

Setup ENDS by printing a ready-to-paste `/goal` block in the canonical shape (verdict-schema.md, Canonical emitted /goal block), specialising the bracketed slots for the chosen binding, then STOPS. The external judge stays independent; converge NEVER arms `/goal` itself. `/goal` takes a natural-language CONDITION; never invent a `/goal seal ...` subcommand. The emitted goal requires all of:

1. `SEAL: ACCEPTED` quoted verbatim by the conductor from a FRESH auditor return, stated to be the MOST RECENT such line AND to post-date the last change to the artefact (no pre-approval; a stale SEAL never fires the goal).
2. The producer's separate completion statement (a signal, not an approval).
3. The round cap (default 4).
4. The non-decreasing-findings escape clause: if total findings do not fall for 2 consecutive rounds, emit `ESCALATE` rather than looping further.

Print the block, then stop. The human pastes `/goal` to arm the engine.

## Constraints

- **NEVER** arm `/goal` or `/loop` yourself; print the block and stop (DEC-R2).
- **NEVER** author a VERDICT or SEAL token as conductor or producer; only reviewer subagents emit them, the conductor quotes verbatim.
- **NEVER** reuse a round reviewer as the seal auditor; the seal is always a FRESH holistic agent.
- **NEVER** pass producer reasoning, self-assessment, or prior verdicts into a review dispatch (reviewer isolation).
- **NEVER** let a producer self-certify tests; the reviewer runs the gates.
- **NEVER** invoke converge from a `w-*` worker; only meta and orch have spawn authority.
- **NEVER** exceed 5 workers per Agent-tool batch, or bundle uncertain calls with safe ones.

## Cross-References

- Token protocol SOT: `~/.claude/skills/_shared/verdict-schema.md`
- Dispatch contract + model split: `~/.claude/skills/_shared/dispatch-contract.md`
- Reviewer resolution: `/review-dispatch` (artifact class to w- type + rubric + effort)
- Swarm quality gates: `~/.claude/rules/40-swarm-quality-gates.md` (R-1..R-4, cross-reference only)
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
