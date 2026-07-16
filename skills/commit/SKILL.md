---
name: commit
description: "Use when drafting a conventional commit message (read-only, does not commit)"
category: workflow
user-invocable: true
argument-hint: "[optional message override]"
allowed-tools: Bash, Read
---

# Commit Message Draft

Draft a conventional commit message from the staged diff and stop. This skill is
read-only: it never runs `git add` or `git commit`. The agent or the owner takes
the drafted message and runs the actual commit separately, per rules/00-universal.md
§ Git Discipline (the `/git` policy toggle gates whether that commit is allowed to
run at all; see `skills/git/SKILL.md`).

## Steps

1. Run `git status` to see all changes
2. Run `git diff --cached` to analyze staged changes (if nothing staged, run `git diff` for unstaged and note that nothing is staged yet)
3. Auto-detect change type from diff:
   - New files with business logic -> `feat:`
   - Modified files fixing behavior -> `fix:`
   - Test files only -> `test:`
   - Markdown/docs only -> `docs:`
   - Config/deps/build files -> `chore:`
   - Restructuring without behavior change -> `refactor:`
   - Formatting only -> `style:`
   - CI/CD files -> `ci:`
   - Performance improvements -> `perf:`
4. Draft a concise commit message (1-2 sentences, focus on WHY not WHAT)
5. **Format**: `<type>(<optional scope>): <description>` -- enforce conventional format
6. If $ARGUMENTS provided, use it as the message instead (still validate format)
7. Present the drafted message as this skill's output. Do not stage, do not commit, do not toggle any permission.

## Guards

- Flag if the diff appears to touch .env, credentials, or secret files (informational only; this skill does not block or stage anything)
- Flag WSL permission-only diffs (`git diff --cached --summary` showing mode changes with 0 insertions/deletions) so the caller knows to exclude them when they stage
