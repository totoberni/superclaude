---
name: sanity-check
description: "Detect test-weakening, scope violations, and drift in orch work."
category: meta
user-invocable: true
argument-hint: "<orch-name>"
context: fork
agent: w-reviewer
---

Sanity-check the changes made by orch **$ARGUMENTS**.

## Setup

1. Read `~/.claude/comms/$ARGUMENTS/bootstrap.md` to get the orch's repo path, branch, and file scope
2. Read `~/.claude/comms/$ARGUMENTS/directives.md` to understand what the orch was told to do
3. Read `~/.claude/plans/vps-migration/state-${ARGUMENTS#orch-example-project-}.md` to see what the orch claims it did

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

For each finding:
```
[SEVERITY] File:line — Description
  Before: <old code>
  After:  <new code>
  Verdict: <OK if justified, REVERT if wrong, INVESTIGATE if unclear>
```

Severity levels: OK (correct change), CONCERN (needs justification), BAD (should be reverted), SCOPE (outside orch's mandate).

End with a summary table and overall verdict (CLEAN / NEEDS_FIXES / BLOCK_MERGE).
