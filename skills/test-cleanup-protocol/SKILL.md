---
name: test-cleanup-protocol
description: "Use when cleaning test env before a run; clears pycache, dirtied files, git."
category: testing
user-invocable: false
allowed-tools: Bash, Read, Grep, Glob
---

# Test Cleanup Protocol

Run BEFORE forming any theory about why a test fails. Mandatory step before diagnosis.

## Procedure

### Step 1: Nuke Stale Bytecode

The target is every directory named exactly `__pycache__` inside `<repo>`, excluding anything under a virtual environment (`.venv/`, `venv/`) or `node_modules/`.

1. Enumerate them with the Glob tool: pattern `**/__pycache__` with `path` set to the absolute `<repo>` path. Read the resulting list.
2. Delete each listed directory by its full, named path. Every path you delete must end in `__pycache__` and sit under `<repo>`. Do not use a recursive wildcard delete, and do not delete a parent directory to save time.

Stale `.pyc` from worktree runs causes 30+ phantom test failures that look like real bugs. ALWAYS clean before diagnosing.

### Step 2: Restore Test-Dirtied Tracked Files (if applicable)

If the test run regenerates or rewrites tracked files (generated docs, fixtures, snapshots), it may have dirtied the working tree. Restore SELECTIVELY (NEVER `git checkout -- docs/` broad — list specific paths from your task brief's Known Pitfalls):

```bash
git -C <repo> checkout -- <paths-from-task-brief>
```

### Step 3: Check Git Status

```bash
git -C <repo> status --short
```

Look for unexpected modifications. If you see anything you didn't intend, STOP and investigate before re-running tests.

### Step 4: Re-Run

After cleanup, re-run the failing tests. If they pass after cleanup, the failure was environmental — note it in your report and move on. Do NOT waste time diagnosing phantom failures.

## When To Use

- Before any test diagnostic in `w-tester`, `w-debugger`
- After any test run that writes into the working tree
- When tests fail unexpectedly after worktree operations

## Cross-References

- `~/.claude/rules/21-domain-gotchas.md`
- `~/.claude/agents/w-tester.md` and `~/.claude/agents/w-debugger.md` (which load this skill)
