---
name: sanity-check
description: "Use when auditing a worker's or swarm's changes for test-weakening or scope drift."
category: meta
user-invocable: true
argument-hint: "<repo-path> <branch-or-commit-range> [task-brief-path]"
context: fork
agent: w-reviewer
---

Sanity-check the changes identified by **$ARGUMENTS**.

## Setup

1. Parse `$ARGUMENTS` for the absolute repo path and the branch or commit range under audit
2. Read the task brief the worker (or swarm) was given, to understand what it was told to do and its declared file scope. If a brief path is supplied, read it; otherwise ask the caller for the brief or the declared scope
3. Read the worker's `STATUS:` return (or its checkpoint file, if one was named) to see what it claims it did. If it is absent, fall back to the commit range and note the missing return.

## Identify the changes

Determine the fork point and diff:
- If the work is on a worktree branch: `git -C <repo> log --oneline <fork-commit>..HEAD` and `git -C <repo> diff <fork-commit>..HEAD`
- If on the main branch: use the supplied commit range

## Check each change against these criteria

### 1. Test Weakening (HIGH priority)
- `== N` changed to `>= N` without justification (loosened assertions)
- `assert X` replaced with `pytest.skip()` (hiding failures)
- Expected values changed to match broken output instead of fixing the code
- Parametrized test matrices with items removed

### 2. Scope Violations
- Files modified outside the declared file scope (from the task brief)
- `conftest.py` touched by a worker that doesn't own it
- Test files belonging to another worker modified

### 3. Generated File Pollution
- `docs/architecture/`, `model.json`, `diagram.mmd`, `index.html` regenerated from a worktree (reflects worktree state, not real system)
- Any file where the diff is disproportionately large (>1000 lines) relative to the logical change

### 4. Semantic Drift
- Agent/model/provider names removed from assertion sets — verify they were actually removed from the codebase (check `git log --all --oneline -- <path>` for removal commits)
- Test descriptions/names changed to be vaguer (e.g., `test_has_9_agents` → `test_has_agents`)
- Comments weakened or removed

### 5. Behavioral Changes to Production Code
- Source files under `services/` or `scripts/` modified when the task brief only asked for test fixes
- New functionality added (scope creep)
- Error handling changed

## Output Format

Line 1 of the return is the machine-readable token, on every run, so the caller can read the verdict even when the tail is truncated (verdict-first, `verdict-schema.md`):

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N
```

The human-readable body follows beneath it. For each finding:
```
[SEVERITY] File:line — Description
  Before: <old code>
  After:  <new code>
  Verdict: <OK if justified, REVERT if wrong, INVESTIGATE if unclear>
```

Severity levels: OK (correct change), CONCERN (needs justification), BAD (should be reverted), SCOPE (outside the worker's mandate).

End with a summary table and overall verdict (CLEAN / NEEDS_FIXES / BLOCK_MERGE).

The counts map onto the verdict-schema severity row:

| sanity-check verdict tier | token field |
|---|---|
| BLOCK_MERGE (e.g. a BAD revert, a scope violation, or any anti-hacking hit) | `blocking` |
| NEEDS_FIXES (e.g. a CONCERN awaiting justification) | `major` |
| non-gating note | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`.

## Single-pass review

A sanity-check is one dispatch and one verdict. It is isolated (artefact + diff + rubric only; never the producer's reasoning or a prior clean verdict) and examines the CURRENT state with fresh evidence gathered in this review (no pre-approval, `verdict-schema.md`). The caller reads the `VERDICT:` line, addresses the findings, and stops; a re-review happens only if the owner or meta judges it necessary. Only sanity-check authors the token, never the caller or the worker under review; a caller that relays the result quotes the VERDICT line verbatim.

**Anti-hacking sweep.** Categories 1 (Test Weakening) and 4 (Semantic Drift) ARE the anti-hacking sweep that `verdict-schema.md` marks as an automatic `blocking` finding in any scope: test special-casing, weakened assertions, expected values matched to broken output, silently removed coverage. Any hit forces `VERDICT: REWORK` with `blocking>=1` regardless of the rest of the diff, and it runs on every review without being asked.

## Cross-References

- Token protocol and severity map (BLOCK_MERGE/NEEDS_FIXES/CLEAN to blocking/major/minor): `~/.claude/skills/_shared/verdict-schema.md`
