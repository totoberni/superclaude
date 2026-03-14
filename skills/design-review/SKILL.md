---
name: design-review
description: "Invoke a design review of frontend changes. /design-review [PR#|branch|path]"
category: meta
user-invocable: true
disable-model-invocation: true
argument-hint: "[PR-number | branch-name | file-path]"
allowed-tools: Read, Glob, Grep, Bash, Agent
---

# design-review — Frontend Design Review

Spawn a w-design-reviewer agent to conduct a multi-phase design review.

**Arguments**: $ARGUMENTS — what to review (optional)

## Steps

### 1. Determine what to review

Parse `$ARGUMENTS`:

- **If a number** (e.g., `42`): treat as a PR number.
  ```bash
  gh pr diff $ARGUMENTS -- '*.tsx' '*.jsx' '*.css' '*.scss' '*.html' '*.vue' '*.svelte'
  ```
  Also get the PR description:
  ```bash
  gh pr view $ARGUMENTS --json title,body
  ```

- **If a branch name** (e.g., `feature/new-ui`): diff against main.
  ```bash
  git diff main...$ARGUMENTS -- '*.tsx' '*.jsx' '*.css' '*.scss' '*.html' '*.vue' '*.svelte'
  ```

- **If a file path** (e.g., `src/components/Button.tsx`): review that specific file.

- **If empty**: find uncommitted frontend changes.
  ```bash
  git diff --name-only -- '*.tsx' '*.jsx' '*.css' '*.scss' '*.html' '*.vue' '*.svelte'
  git diff --cached --name-only -- '*.tsx' '*.jsx' '*.css' '*.scss' '*.html' '*.vue' '*.svelte'
  ```

If no frontend files found, tell the user: "No frontend changes detected. Specify a PR number, branch, or file path."

### 2. Collect context

Gather:
- The diff (from step 1)
- List of changed file paths
- PR description (if reviewing a PR)

### 3. Spawn w-design-reviewer

Delegate to the `w-design-reviewer` agent with:
- The full diff content
- List of absolute file paths to review
- PR/branch context if available
- Instruction: "Run all 7 phases. Use `[Blocker]/[High]/[Medium]/[Nit]` triage format. End with a verdict."

### 4. Return the review report

Display the agent's review output directly to the user. Do not summarize or filter — the full report is the deliverable.

## Notes

- The w-design-reviewer currently works in code-only mode (no Playwright MCP)
- For git commands that need a specific repo, use `git -C <repo-path>`
- Frontend file extensions: `.tsx`, `.jsx`, `.css`, `.scss`, `.html`, `.vue`, `.svelte`
