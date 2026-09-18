---
name: pr
description: "Use when creating a GitHub pull request with a structured summary"
category: workflow
user-invocable: true
argument-hint: "[base-branch]"
allowed-tools: Bash, Read, Write
---

# PR Workflow

Create a GitHub pull request for the current branch.

## Steps

1. Check current branch and remote tracking: `git status` + `git branch -vv`
2. Determine base branch ($ARGUMENTS or default: main)
3. Analyze ALL commits since divergence: `git log $0...HEAD` + `git diff $0...HEAD`
4. Push to remote if needed: `git push -u origin HEAD`
5. Draft PR:
   - Title: under 70 chars, descriptive
   - Body: Summary bullets + Test plan checklist
6. Create PR. Write the body to `~/.claude/tmp/pr-body.md` with the Write tool:
   ```markdown
   ## Summary
   <1-3 bullet points>

   ## Test plan
   - [ ] ...

   Co-Authored-By: Claude <noreply@anthropic.com>
   ```
   then pass it as a file (works identically in Git Bash and PowerShell):
   ```bash
   gh pr create --title "<title>" --body-file ~/.claude/tmp/pr-body.md
   ```
7. Return the PR URL

## Pre-publish self-check

`pr` is a ONE-SHOT ACTION: a single invocation drafts and creates the pull request (steps 1-7 above). It has no REWORK cycle.

Before running `gh pr create` (step 6), run ONE optional single self-check on the drafted description: does the Summary accurately reflect the actual diff gathered in step 3, and does the Test plan checklist cover the changed surface? This is a single pass: one fresh look before publishing.

Reviewing the underlying CODE is a separate job, run via `/fix-issue` or `/review`; `pr` itself only publishes what already exists.
