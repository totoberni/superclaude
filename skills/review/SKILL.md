---
name: review
description: "Invoke w-reviewer on staged changes. Modes: general/infra/security."
category: workflow
user-invocable: true
argument-hint: "[mode] [file-or-commit-range] [--loop]"
context: fork
agent: w-reviewer
---

Review the following changes for code quality, security, and best practices:

Target: $ARGUMENTS

If no target specified:
1. Run `git diff --cached` for staged changes
2. If nothing staged, run `git diff` for unstaged changes
3. If no changes, run `git log -1 --format=%H` and review the last commit

Mode selection (first argument, optional):
- `/review` — auto-detect mode from file paths in the diff
- `/review security` — force security mode (STRIDE checklists)
- `/review infra` — force infra mode (infra-security checklist)
- `/review security src/auth/` — security mode on specific path

## Output Format

Line 1 of the return is the machine-readable token, on every run, so a driver can decide the round even when the tail is truncated (verdict-first, `verdict-schema.md`):

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K
```

The human-readable findings follow beneath it, organised by priority (Blocker > High > Medium > Nit), each with a specific file:line reference. The counts map onto the verdict-schema severity row: Blocker to `blocking`, High to `major`, Medium and Nit to `minor`. `VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`. `round=K` is the loop round in progress (`round=1` for a one-shot review outside a loop).

## Loop integration (converge)

review is the `code-small` / `code-large` reviewer in `/review-dispatch`, resolved to w-reviewer with the `code-quality` rubric (infra mode resolves instead to the `infra` class with the infra-security rubric, still w-reviewer; the loop path below is the code path). This section states how review plugs into the `/converge` engine; the base one-shot review behaviour above is unchanged. Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill); this skill's single invocation forks to w-reviewer as the per-round code reviewer only, never the loop driver, and never seals itself.

### (a) As a reviewer (invoked each round by /converge)

`/converge` and `/review-dispatch` resolve review as the `code-small` (<=3 files, sonnet) or `code-large` / architectural (opus) reviewer and dispatch it once per round against the current diff, isolated (artefact + diff + rubric only; never the producer's reasoning or a prior clean verdict) and re-examining the CURRENT state with fresh evidence THIS round (no pre-approval, `verdict-schema.md`).

Every run emits the machine token on line 1 (see Output Format), in ADDITION to the human-readable findings, so the loop is machine-decidable. The counts map onto the verdict-schema severity row:

| review priority | token field |
|---|---|
| Blocker (or any anti-hacking hit) | `blocking` |
| High | `major` |
| Medium, Nit | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`. `round=K` is the loop round in progress (`round=1` for a one-shot review outside a loop). The conductor quotes the VERDICT line verbatim into the ledger; only review authors the token, never the conductor or the producer under review.

**Anti-hacking sweep.** A Blocker fires automatically, in any scope, for the patterns `verdict-schema.md` marks as an automatic `blocking` finding: test special-casing, weakened assertions, harness escapes (forced exit-0), skipped or deleted coverage, diagnostic theatre in infra tests or health scripts. Any hit forces `VERDICT: REWORK` with `blocking>=1` regardless of the rest of the diff, and it runs every round without being asked.

### (b) --loop (conductor-driven shorthand)

**Authority: meta + orch only**, the same as `/converge`. A `w-*` worker cannot drive a loop, so `--loop` invoked from a worker is a no-op error; the base one-shot review carries no such restriction.

review does NOT reimplement a bespoke self-sealing loop, and never seals itself. To iterate to CLEAN, the conductor (meta/orch) runs `/converge` with the appropriate code artifact-class (`code-small` for <=3 files, `code-large` for a larger or architectural diff); review is the round reviewer each round (emitting its VERDICT line), and `/converge` supplies the terminal `SEAL: ACCEPTED` from a FRESH auditor of a different identity than any round reviewer (two-token protocol; review never seals itself). `--loop` is therefore a shorthand that prints the `/converge` invocation for the conductor to run; it never self-arms (DEC-R2).

The shorthand prints this, then STOPS:

```
/converge <diff-or-artefact under review> --binding B1
```

resolved with the code artifact-class (`code-small` or `code-large`), which selects review as the per-round reviewer via `/review-dispatch`. `/converge` then owns every loop mechanic (rounds, ledger, the 8 loop rules, caps, goal-string emission) and emits the `/goal` block whose clause 1 requires a `SEAL: ACCEPTED blocking=0 major=0 minor=0` (nits=0 at the gate or strict bar) quoted verbatim from a FRESH holistic auditor whose identity differs from every round reviewer, never from review's own `VERDICT: CLEAN`.

## Cross-References

- Token protocol and severity map (Blocker/High/Medium to blocking/major/minor): `~/.claude/skills/_shared/verdict-schema.md`
- Convergence engine that consumes this verdict each round: `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`code-small` / `code-large` class to the code-quality rubric): `~/.claude/skills/review-dispatch/SKILL.md`
