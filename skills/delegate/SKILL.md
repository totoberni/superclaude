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
