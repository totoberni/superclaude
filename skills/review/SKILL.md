---
name: review
description: "Invoke w-reviewer on staged changes. Modes: general/infra/security."
category: workflow
user-invocable: true
argument-hint: "[mode] [file-or-commit-range]"
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

Provide findings organized by priority (Blocker > High > Medium > Nit).
Include specific file:line references.
