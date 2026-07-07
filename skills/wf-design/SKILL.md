---
name: wf-design
description: "Use when driving a research experimental design to a hostile-review methodology seal. Meta+orch only."
category: workflow
user-invocable: true
argument-hint: "<phenomenon> [--rounds N] [--strict]"
allowed-tools: Read, Write, Edit, Bash, Agent, Skill
---

# wf-design

Convergence binding for a research experimental design. Each round a producer drafts or revises the design document against the 13 `/research design` steps; a hostile reviewer audits it for methodology soundness. Rounds repeat until a FRESH review returns a clean `SEAL` in the same round as the producer's own completion statement. wf-design configures and runs the rounds; the printed `/goal` block lets the external engine enforce the exit. The skill never arms the engine itself.

**Authority**: meta + orch only. Workers (`w-*`) cannot spawn the producer or the reviewer, so invoking wf-design from a worker is a no-op error. The conductor (meta or orch) owns the loop, quotes verdicts, and maintains the ledger.

## What this binds

wf-design is a thin binding of `/converge` on binding **B1** (goal-sealed convergence). It fixes the three generic converge slots to a research experimental design:

- **Artifact**: one design document, addressing all 13 steps of the `design` subcommand of `/research` (`research/references/design.md`) for a stated phenomenon.
- **Producer (per round)**: a single producer worker that drafts (round 1) or revises against the punch list (later rounds) by running the `design` subcommand end to end; a design missing a step is not round-complete.
- **Reviewer (per round)**: `w-hostile-reviewer`, resolved via `/review-dispatch` on the `methodology` artifact class (never the unrelated `design` row, which resolves to the frontend `w-design-reviewer`), scoped `--scope methodology`; a design has no implementation yet, so the technical gauntlet stays out of scope.

Loop mechanics (round order, round ledger, two-token protocol, caps, post-compaction requote) are inherited from `/converge`; this file states only the design-specific artifact/producer/reviewer slots and the goal predicate.

## Loop body (per round)

Converge's five steps, filled with design-specific content:

1. **PRODUCE / REVISE**: producer drafts (round 1) or revises against the punch list (later rounds) the design document per `research/references/design.md`, against `<phenomenon>`; all 13 steps must be addressed, a design missing one is not round-complete.
2. **PERSIST**: producer writes the design document to disk; the conductor appends a ledger entry (round, delta, open-findings count) before review runs.
3. **REVIEW**: resolved via `/review-dispatch` on the `methodology` artifact class, `--scope methodology` (artifact + diff + rubric only; reviewer isolation). Resolves to `w-hostile-reviewer`; it emits a `VERDICT` line.
4. **REPORT**: the conductor quotes the reviewer's token line verbatim, `VERDICT` mid-loop, `SEAL` on the sealing round.
5. **TRIAGE**: accept or contest each finding with evidence (file:line, a named principle and clause, or an expected-vs-actual re-run); accepted findings become the next round's punch list.

## Goal predicate

Converges on `/converge`'s standard dual-condition exit: a FRESH `w-hostile-reviewer` (scope `methodology`) returns `SEAL: ACCEPTED blocking=0 major=0 minor=0` (`nits=0` additionally required at the gate or strict bar; `--strict` further requires two consecutive clean SEALs, see `verdict-schema.md` Bar levels), quoted verbatim by the conductor, together with the producer's own completion statement (STATUS: DONE) in the same round. The design is sealed only when the methodology gauntlet, run cold, finds nothing left to block, weaken, or (at the gate or strict bar) merely polish.

## Emitted /goal block

Setup ends by printing a ready-to-paste `/goal` block, then stops (DEC-R2: the external judge stays independent; wf-design never arms `/goal` itself). Template:

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH w-hostile-reviewer return (scope methodology), is the MOST RECENT such line, and post-dates the last change to the design document, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); (2) the producer has separately stated completion (STATUS: DONE). If review rounds exceed <N> (from --rounds, else 4), or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Print the block, then stop. The human pastes `/goal` to arm the engine.

## Constraints

- **NEVER** pass `design` as the `/review-dispatch` artifact class; that row resolves to the frontend `w-design-reviewer`. Use `methodology`.
- **NEVER** let the round-1 reviewer double as the sealing auditor; the seal is always a FRESH `w-hostile-reviewer` pass.
- **NEVER** widen `--scope` beyond `methodology`; there is no implementation yet for the technical gauntlet to audit.
- **NEVER** run the reviewer without ultrathink and max effort engaged; `hostile-review` disables itself otherwise.
- **NEVER** skip any of the 13 `/research design` steps; a design missing one is not round-complete.
- **NEVER** arm `/goal` yourself; print the block and stop (DEC-R2).
- **NEVER** invoke wf-design from a `w-*` worker; only meta and orch hold spawn authority.

## Cross-References

- Loop engine + round mechanics: `/converge` (binding B1)
- Reviewer resolution (methodology class): `/review-dispatch`
- Per-round design auditor: `/hostile-review` (run by `w-hostile-reviewer`)
- Design protocol (13 steps): `~/.claude/skills/research/references/design.md`
- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Campaign plan: `~/.claude/plans/wf-skills/plan.md`
