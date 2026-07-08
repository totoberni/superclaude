---
name: pr
description: "Create a GitHub pull request with structured summary using gh CLI."
category: workflow
user-invocable: true
argument-hint: "[base-branch]"
allowed-tools: Bash, Read
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
6. Create PR:
   ```bash
   gh pr create --title "<title>" --body "$(cat <<'EOF'
   ## Summary
   <1-3 bullet points>

   ## Test plan
   - [ ] ...

   Co-Authored-By: Claude <noreply@anthropic.com>
   EOF
   )"
   ```
7. Return the PR URL

## Loop integration (converge)

`pr` is a ONE-SHOT ACTION: a single invocation drafts and creates the pull request (steps 1-7 above). It has no round-by-round REWORK cycle and emits no SEAL; a full `/converge` loop does not apply to the act of publishing the PR itself.

Before running `gh pr create` (step 6), run ONE optional single self-check on the drafted description: does the Summary accurately reflect the actual diff gathered in step 3, and does the Test plan checklist cover the changed surface? This is a single pass, not a loop: one fresh look before publishing, no VERDICT/REWORK cycling.

Iterating the underlying CODE to a sealed quality bar is `/converge`'s job, run separately via `/fix-issue` or `/review`; `pr` itself only publishes what already exists and stays one-shot regardless.

Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill), per `converge/SKILL.md`'s Conductor context convention.
