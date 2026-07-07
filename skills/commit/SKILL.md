---
name: commit
description: "Creates a conventional commit with auto-detected type and TDD check."
category: workflow
user-invocable: true
argument-hint: "[optional message override]"
allowed-tools: Bash, Read
---

# Commit Workflow

Create a conventional commit for staged changes.

## Steps

1. Run `git status` to see all changes
2. Run `git diff --cached` to analyze staged changes (if nothing staged, run `git diff` for unstaged)
3. **TDD check**: check if the session's TDD counter (`~/.claude/session-timers/*.tdd`) is >0. If edits happened without tests, warn before committing.
4. Auto-detect change type from diff:
   - New files with business logic → `feat:`
   - Modified files fixing behavior → `fix:`
   - Test files only → `test:`
   - Markdown/docs only → `docs:`
   - Config/deps/build files → `chore:`
   - Restructuring without behavior change → `refactor:`
   - Formatting only → `style:`
   - CI/CD files → `ci:`
   - Performance improvements → `perf:`
5. Draft a concise commit message (1-2 sentences, focus on WHY not WHAT)
6. **Format**: `<type>(<optional scope>): <description>` — enforce conventional format
7. If $ARGUMENTS provided, use it as the message instead (still validate format)
8. Present the message for the user's approval before committing
9. Stage relevant files by name (never `git add -A`)
10. Check for WSL permission-only diffs (`git diff --cached --summary` for mode changes with 0 insertions/deletions) — unstage those
11. Commit with:
    ```
    git commit -m "$(cat <<'EOF'
    <type>: <message>

    Co-Authored-By: Claude <noreply@anthropic.com>
    EOF
    )"
    ```
12. Run `git status` to verify

## Guards

- Never commit .env, credentials, or secret files. Warn if detected.
- Never commit mode-only diffs (WSL permission changes)
- Warn if no tests ran this session (TDD counter > 0)
