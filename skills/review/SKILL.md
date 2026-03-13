---
name: review
description: "Invokes the w-reviewer agent on staged or recent changes in an isolated context."
user-invocable: true
argument-hint: "[file-or-commit-range]"
context: fork
agent: w-reviewer
---

Review the following changes for code quality, security, and best practices:

Target: $ARGUMENTS

If no target specified:
1. Run `git diff --cached` for staged changes
2. If nothing staged, run `git diff` for unstaged changes
3. If no changes, run `git log -1 --format=%H` and review the last commit

Provide findings organized by priority (Critical > Warnings > Suggestions).
Include specific file:line references.
