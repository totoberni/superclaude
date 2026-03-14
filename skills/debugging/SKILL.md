---
name: debugging
description: "Systematic debugging methodology. Loaded by w-debugger agent."
category: meta
user-invocable: false
---

# Systematic Debugging

This skill is loaded by the `w-debugger` agent. See `~/.claude/agents/w-debugger.md` for the full 4-phase methodology.

## Reference Materials

- `references/root-cause-tracing.md` — Trace bugs backward through call stack to find original trigger
- `scripts/find-polluter.sh` — Bisection script to find which test creates unwanted state

## Quick Reference

| Phase | Key Activity | Gate |
|-------|-------------|------|
| 1. Reproduce & Observe | Run failing command, read FULL output | Cannot propose fixes until done |
| 2. Narrow the Scope | Binary search, check gotchas | Must have specific file/function |
| 3. Identify Root Cause | Read code path, verify theory | Theory must be testable |
| 4. Fix & Verify | Minimal fix, run original + suite | Original command must pass |
