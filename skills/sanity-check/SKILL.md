---
name: sanity-check
description: "Detect test-weakening, scope violations, and drift in orch work."
category: meta
user-invocable: true
argument-hint: "<orch-name> [--loop]"
context: fork
agent: w-reviewer
---

Sanity-check the changes made by orch **$ARGUMENTS**.

## Setup

1. Read `~/.claude/comms/$ARGUMENTS/bootstrap.md` to get the orch's repo path, branch, and file scope
2. Read `~/.claude/comms/$ARGUMENTS/directives.md` to understand what the orch was told to do
3. Read `~/.claude/plans/vps-migration/state-${ARGUMENTS#orch-<project>-}.md` to see what the orch claims it did

## Identify the changes

Determine the orch's fork point and diff:
- If the orch is on a worktree branch: `git -C <repo> log --oneline <fork-commit>..HEAD` and `git -C <repo> diff <fork-commit>..HEAD`
- If on the main branch: use the commit range from the state file

## Check each change against these criteria

### 1. Test Weakening (HIGH priority)
- `== N` changed to `>= N` without justification (loosened assertions)
- `assert X` replaced with `pytest.skip()` (hiding failures)
- Expected values changed to match broken output instead of fixing the code
- Parametrized test matrices with items removed

### 2. Scope Violations
- Files modified outside the orch's declared file scope (from directive)
- `conftest.py` touched by an orch that doesn't own it
- Test files belonging to another orch modified

### 3. Generated File Pollution
- `docs/architecture/`, `model.json`, `diagram.mmd`, `index.html` regenerated from a worktree (reflects worktree state, not real system)
- Any file where the diff is disproportionately large (>1000 lines) relative to the logical change

### 4. Semantic Drift
- Agent/model/provider names removed from assertion sets — verify they were actually removed from the codebase (check `git log --all --oneline -- <path>` for removal commits)
- Test descriptions/names changed to be vaguer (e.g., `test_has_9_agents` → `test_has_agents`)
- Comments weakened or removed

### 5. Behavioral Changes to Production Code
- Source files under `services/` or `scripts/` modified when the directive only asked for test fixes
- New functionality added (scope creep)
- Error handling changed

## Output Format

Line 1 of the return is the machine-readable token, on every run, so a driver can decide the round even when the tail is truncated (verdict-first, `verdict-schema.md`):

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K
```

The human-readable body follows beneath it. For each finding:
```
[SEVERITY] File:line — Description
  Before: <old code>
  After:  <new code>
  Verdict: <OK if justified, REVERT if wrong, INVESTIGATE if unclear>
```

Severity levels: OK (correct change), CONCERN (needs justification), BAD (should be reverted), SCOPE (outside orch's mandate).

End with a summary table and overall verdict (CLEAN / NEEDS_FIXES / BLOCK_MERGE).

## Loop integration (converge)

sanity-check is the `test-integrity` reviewer in `/review-dispatch`; the five-category checklist above is its rubric. This section states how it plugs into the `/converge` engine; the base one-shot reviewer behaviour above is unchanged. Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill); sanity-check forks to w-reviewer as the per-round test-integrity reviewer only, never the loop driver, and never seals itself.

### (a) As a reviewer (invoked each round by /converge)

`/converge` and `/review-dispatch` resolve sanity-check as the `test-integrity` reviewer and dispatch it once per round against the current diff, isolated (artefact + diff + rubric only; never the producer's reasoning or a prior clean verdict) and re-examining the CURRENT state with fresh evidence THIS round (no pre-approval, `verdict-schema.md`).

Every run emits the machine token on line 1 (see Output Format), in ADDITION to the human-readable per-finding blocks and summary table, so the loop is machine-decidable. The counts map onto the verdict-schema severity row:

| sanity-check verdict tier | token field |
|---|---|
| BLOCK_MERGE (e.g. a BAD revert, a scope violation, or any anti-hacking hit) | `blocking` |
| NEEDS_FIXES (e.g. a CONCERN awaiting justification) | `major` |
| non-gating note | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`. `round=K` is the loop round in progress (`round=1` for a one-shot review outside a loop). The conductor quotes the VERDICT line verbatim into the ledger; only sanity-check authors the token, never the conductor or the orch under review.

**Anti-hacking sweep.** Categories 1 (Test Weakening) and 4 (Semantic Drift) ARE the anti-hacking sweep that `verdict-schema.md` marks as an automatic `blocking` finding in any scope: test special-casing, weakened assertions, expected values matched to broken output, silently removed coverage. Any hit forces `VERDICT: REWORK` with `blocking>=1` regardless of the rest of the diff, and it runs every round without being asked.

### (b) --loop (conductor-driven shorthand)

**Authority: meta + orch only**, the same as `/converge`. A `w-*` worker cannot drive a loop, so `--loop` invoked from a worker is a no-op error; the base one-shot review carries no such restriction.

sanity-check does NOT reimplement a bespoke self-sealing loop, and never seals itself. To iterate to CLEAN, the conductor (meta/orch) runs `/converge` with artifact-class `test-integrity`; sanity-check is the round reviewer each round (emitting its VERDICT line), and `/converge` supplies the terminal `SEAL: ACCEPTED` from a FRESH auditor of a different identity than any round reviewer (two-token protocol; sanity-check never seals itself). `--loop` is therefore a shorthand that prints the `/converge` test-integrity invocation for the conductor to run; it never self-arms (DEC-R2).

The shorthand prints this, then STOPS:

```
/converge <diff-or-artefact under review> --binding B1
```

resolved with artifact-class `test-integrity`, which selects sanity-check as the per-round reviewer via `/review-dispatch`. `/converge` then owns every loop mechanic (rounds, ledger, the 8 loop rules, caps, goal-string emission) and emits the `/goal` block whose clause 1 requires a `SEAL: ACCEPTED blocking=0 major=0 minor=0` (nits=0 at the gate or strict bar) quoted verbatim from a FRESH holistic auditor whose identity differs from every round reviewer, never from sanity-check's own `VERDICT: CLEAN`.

## Cross-References

- Token protocol and severity map (BLOCK_MERGE/NEEDS_FIXES/CLEAN to blocking/major/minor): `~/.claude/skills/_shared/verdict-schema.md`
- Convergence engine that consumes this verdict each round: `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`test-integrity` class to this rubric): `~/.claude/skills/review-dispatch/SKILL.md`
