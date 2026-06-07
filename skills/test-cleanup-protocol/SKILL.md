---
name: test-cleanup-protocol
description: "Pre-test environment cleanup: __pycache__, compose host files, git status check"
category: testing
user-invocable: false
allowed-tools: Bash, Read, Grep
---

# Test Cleanup Protocol

Run BEFORE forming any theory about why a test fails. Mandatory step before diagnosis.

## Procedure

### Step 1: Nuke Stale Bytecode

```bash
find <repo> -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true
```

Stale `.pyc` from worktree runs causes 30+ phantom test failures that look like real bugs. ALWAYS clean before diagnosing.

### Step 2: Restore Compose-Dirty Host Files (if applicable)

If the project uses Docker Compose with bind-mount volumes, the test run may have dirtied host files. Restore SELECTIVELY (NEVER `git checkout -- docs/` broad — list specific paths from your directive's Known Pitfalls):

```bash
git -C <repo> checkout -- <paths-from-directive>
```

### Step 3: Check Git Status

```bash
git -C <repo> status --short
```

Look for unexpected modifications. If you see anything you didn't intend, STOP and investigate before re-running tests.

### Step 4: Re-Run

After cleanup, re-run the failing tests. If they pass after cleanup, the failure was environmental — note in your RPT and move on. Do NOT waste time diagnosing phantom failures.

## When To Use

- Before any test diagnostic in `w-tester`, `w-debugger`, `orch`
- After any compose/docker test run
- When tests fail unexpectedly after worktree operations

## Cross-References

- `~/.claude/rules/21-domain-gotchas.md` § Compose / Docker Test Hygiene, § WSL File Permissions
- `~/.claude/agents/orch.md` § Test Failure Protocol (which loads this skill)
