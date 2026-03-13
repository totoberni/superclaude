---
name: commit
description: "Creates a well-formed conventional commit by analyzing staged changes and drafting a message."
user-invocable: true
disable-model-invocation: true
argument-hint: "[optional message override]"
allowed-tools: Bash, Read
---

# Commit Workflow

Create a conventional commit for staged changes.

## Steps

1. Run `git status` to see all changes
2. Run `git diff --cached` to analyze staged changes (if nothing staged, run `git diff` for unstaged)
3. Analyze the nature of changes:
   - New feature → `feat:`
   - Bug fix → `fix:`
   - Tests → `test:`
   - Documentation → `docs:`
   - Refactoring → `refactor:`
   - Build/deps → `chore:`
4. Draft a concise commit message (1-2 sentences, focus on WHY not WHAT)
5. If $ARGUMENTS provided, use it as the message instead
6. Present the message for the user's approval before committing
7. Stage relevant files by name (never `git add -A`)
8. Commit with:
   ```
   git commit -m "$(cat <<'EOF'
   <type>: <message>

   Co-Authored-By: Claude <noreply@anthropic.com>
   EOF
   )"
   ```
9. Run `git status` to verify

IMPORTANT: Never commit .env, credentials, or secret files. Warn if detected.
