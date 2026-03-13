---
name: pr
description: "Creates a GitHub pull request with structured title, summary, and test plan using gh CLI."
user-invocable: true
disable-model-invocation: true
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
