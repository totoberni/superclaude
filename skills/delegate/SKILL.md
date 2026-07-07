---
name: delegate
description: "Fresh subagent per task with two-stage review. For orch agents."
category: orchestration
user-invocable: false
---

# Subagent-Driven Delegation

Adapted from obra/superpowers. Fresh subagent per task + two-stage review = high quality, fast iteration.

## Core Pattern

1. **One fresh subagent per task** — preserves context, prevents pollution
2. **Two-stage review** — spec compliance first, then code quality
3. **When workers fail, re-delegate** — do NOT redo their work yourself

## Per-Task Flow

### 1. Dispatch Worker

Choose the right `w-*` worker for the task:

| Worker | Use For |
|--------|---------|
| `w-debugger` | Runtime errors, test failures |
| `w-refactorer` | Extract/rename/inline/simplify |
| `w-merger` | Git merge conflicts |
| `w-reviewer` | Read-only code review |
| `Explore` | Code reconnaissance (read-only) |

Provide in the task description:
- **Absolute paths** to all files (workers run from `~/projects/workspace/`)
- **Full task context** — workers don't read plan.md or state files
- **Explicit file scope** — which files they may read and edit
- **Success criteria** — what "done" looks like
- **Constraints** — what NOT to touch

### 2. Review: Spec Compliance

After worker returns, check:
- Did the output match the task requirements?
- Any missing requirements?
- Any extra work beyond scope?

If spec gaps found: re-delegate with specific fix instructions.

### 3. Review: Code Quality

After spec passes, check:
- Are changes correct and scoped?
- Tests cover the changed code?
- No weakened assertions, added skips, loosened error handling?
- `git diff --stat` shows only expected files?

If quality issues found: re-delegate with specific fix instructions.

### 4. Mark Complete

Only after both reviews pass. Update state file.

## Parallelism

Spawn **up to 5 workers simultaneously** for independent tasks. Launch them in a single message with multiple Agent tool calls. Requirements:
- Tasks must be independent (non-overlapping files)
- Each worker gets full context (they don't share state)
- Verify ALL outputs after all workers return

## Worker Failure Protocol

When a worker fails:
1. Do NOT redo their work yourself (context pollution)
2. Re-delegate with better instructions:
   - Include the error output from the failed attempt
   - Add more context about the expected behavior
   - Narrow the scope if the task was too broad
3. If re-delegation fails: escalate with ESC-NNN

## Anti-Patterns

- **Don't do it yourself** — your context window is more valuable than a worker's
- **Don't skip reviews** — both stages are required (spec THEN quality)
- **Don't dispatch multiple workers to the same files** — conflicts guaranteed
- **Don't trust worker success reports** — verify independently (see `verify` skill)
- **Don't provide partial context** — workers need everything upfront

## Loop integration (converge)

delegate is the orch's per-task PRIMITIVE: one fresh subagent dispatched per task, then the two-stage review (spec compliance first, then code quality) applied to its return. That informal dispatch, then two-stage review, then re-delegate-on-failure cycle IS a convergence loop, and `/converge` is its formalisation. This section states how the primitive plugs into that engine; the base per-task behaviour above is unchanged. Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill); delegate contributes the per-task dispatch and the two-stage review rubric the conductor applies each round, never the loop driver, and never seals itself.

**What `/converge` owns (do not reimplement here).** Rounds, caps (default `--rounds 4`), the round ledger, the two-token verdict protocol, the non-decreasing-findings ESCALATE arm, and no-pre-approval all live in `/converge` and `verdict-schema.md`. One dispatch-and-review is a single round of that loop; iterating multiple rounds to a sealed finish is `/converge`, not a bespoke retry coded into this skill.

**What delegate contributes: the two-stage review rubric.** The unique primitive here is the ordered two-stage gate, and it is the reviewer rubric `/converge` applies each round:

1. **Stage 1, spec compliance** (`Review: Spec Compliance` above): did the return meet every task requirement, with no missing requirement and no out-of-scope extra? A gap fails the round.
2. **Stage 2, code quality** (`Review: Code Quality` above): correct and scoped changes, tests covering the change, and no weakened assertions, added skips, or loosened error handling (the `verdict-schema.md` anti-hacking sweep), with `git diff --stat` showing only expected files. Stage 2 runs only after stage 1 passes.

A round is CLEAN only when BOTH stages pass. The findings map onto the `verdict-schema.md` severity fields:

| delegate finding | token field |
|---|---|
| missing requirement (stage 1), or an anti-hacking hit or scope violation (stage 2) | `blocking` |
| a correctness or missing-test gap awaiting a fix (either stage) | `major` |
| a non-gating polish note | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`, and its punch list becomes the next round's re-delegation (the `Worker Failure Protocol` above IS that re-delegation). Only the reviewer subagent authors the VERDICT line; the conductor quotes it verbatim into the ledger and never authors a token itself.

**The terminal seal is never delegate's.** Both stages clean across the final revision is sealed by `/converge`'s FRESH holistic auditor, whose identity differs from every round reviewer, emitting `SEAL: ACCEPTED` together with the producer's separate `STATUS: DONE` (two independent signals, `verdict-schema.md` dual-condition exit). delegate never self-seals: a round reviewer's `VERDICT: CLEAN` is not a seal, and any change after a SEAL voids it.

To iterate to both-stages-pass, the conductor (meta/orch) runs `/converge` on the code artefact and applies delegate's two-stage gate each round: stage 1 (spec-compliance) is delegate's own rubric; stage 2 (code-quality) is the `code-small` / `code-large` reviewer resolved by `/review-dispatch` to `w-reviewer`. `/converge` supplies the terminal seal. Authority is meta and orch only, the same as `/converge`: a `w-*` worker cannot spawn a reviewer, so it cannot drive the loop.

## Cross-References

- Convergence engine that formalises the retry loop (rounds, ledger, the 8 loop rules, caps, goal-string emission): `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`code-small`/`code-large` to `w-reviewer`, model, rubric, isolation): `~/.claude/skills/review-dispatch/SKILL.md`
- Two-token verdict protocol, bar levels, severity map, no-pre-approval: `~/.claude/skills/_shared/verdict-schema.md`
- Four-part dispatch contract, model split, checkpoint-first: `~/.claude/skills/_shared/dispatch-contract.md`
- Swarm quality gates (R-1..R-4): `~/.claude/rules/40-swarm-quality-gates.md`
