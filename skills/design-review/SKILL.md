---
name: design-review
description: "Use when reviewing the design of frontend changes"
category: meta
user-invocable: true
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
- Instruction: "Run all 7 phases. Use `[Blocker]/[High]/[Medium]/[Nit]` triage format. Line 1 of your return MUST be the verdict-first token `VERDICT: REWORK|CLEAN blocking=N major=N minor=N` (see Output Format), then the full per-phase report."

### 4. Return the review report

Display the agent's review output directly to the user. Do not summarize or filter — the full report is the deliverable.

## Output Format

Line 1 of the review return is the machine-readable token, emitted on every run so the caller can read the verdict even when the tail is truncated (verdict-first, `verdict-schema.md`):

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N
```

The full 7-phase report (with `[Blocker]/[High]/[Medium]/[Nit]` findings) follows beneath the token; that report is the deliverable and is never summarised or filtered. The triage counts map onto the verdict-schema severity row:

| design-review severity | token field |
|---|---|
| `[Blocker]` | `blocking` |
| `[High]` | `major` |
| `[Medium]` | `minor` |
| `[Nit]` | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`. Only the w-design-reviewer authors the token; the caller quotes the VERDICT line verbatim and never rewrites it.

## Single-pass review

A design review is one dispatch and one verdict. The w-design-reviewer is isolated (artefact + diff + rubric only; never the producer's reasoning or a prior clean verdict) and examines the CURRENT state with fresh evidence gathered in this review (no pre-approval, `verdict-schema.md`). The caller reads the `VERDICT:` line, addresses the findings, and stops; a re-review happens only if the owner or meta judges it necessary. Sonnet is the default reviewer model; escalate to opus when the scope is cross-page consistency rather than a single view.

## Notes

- The w-design-reviewer currently works in code-only mode (no Playwright MCP)
- For git commands that need a specific repo, use `git -C <repo-path>`
- Frontend file extensions: `.tsx`, `.jsx`, `.css`, `.scss`, `.html`, `.vue`, `.svelte`

## Cross-References

- Token protocol and severity map (Blocker/High/Medium/Nit to blocking/major/minor): `~/.claude/skills/_shared/verdict-schema.md`
