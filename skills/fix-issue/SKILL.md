---
name: fix-issue
description: "End-to-end GitHub issue fix pipeline. /fix-issue <owner/repo#number>"
user-invocable: true
argument-hint: "<owner/repo#number> or <issue-url>"
allowed-tools: Read, Edit, Write, Bash, Glob, Grep, Agent
---

# fix-issue — GitHub Issue Fix Pipeline

Analyze a GitHub issue, implement a fix, run tests, and commit.

**Arguments**: $ARGUMENTS — issue reference (required)

## Steps

### 1. Verify GitHub CLI authentication

```bash
gh auth status
```

If not authenticated, tell the user: "Run `gh auth login` first — the GitHub CLI is not authenticated." and STOP.

### 2. Parse issue reference

Parse `$ARGUMENTS` for the issue. Supported formats:
- `owner/repo#123`
- `https://github.com/owner/repo/issues/123`
- `#123` (uses current repo context from `gh repo view --json nameWithOwner -q .nameWithOwner`)

Extract `OWNER/REPO` and `NUMBER`.

### 3. Fetch issue details

```bash
gh issue view <NUMBER> --repo <OWNER/REPO> --json title,body,labels,comments
```

Read the title, body, labels, and comments to understand the problem.

### 4. Locate the project repo

Determine which local project under `~/projects/workspace/` corresponds to `OWNER/REPO`:
- Check `gh repo view --json name -q .name` to get the repo name
- Look for a matching directory: `ls ~/projects/workspace/ | grep -i <repo-name>`
- If no match found, tell the user and STOP

Set `REPO_PATH` to the absolute path (e.g., `$HOME/projects/workspace/<repo-dir>`).

### 5. Search codebase for relevant files

Search for keywords from the issue title/body:
- Use Grep with relevant terms against `REPO_PATH`
- If >3 potential file locations, spawn an Explore sub-agent to narrow down

Identify the files that need changes.

### 6. Implement the fix

- Read each target file before editing (universal rule)
- Make minimal, focused changes that address the issue
- Follow the project's existing patterns and conventions

### 7. Run tests

Run the project's test suite covering the changed code. Use a foreground Bash command with a 10-minute timeout:

```bash
# Adjust the test command to the project's stack
# Python: python3 -m pytest <test_file> -v
# Node: npm test -- --testPathPattern=<pattern>
# Use timeout: 600000 on the Bash tool call
```

If tests fail, fix and re-run. If tests fail 3 times, stop and report the failures to the user (per escalation rule).

### 8. Commit the fix

```bash
git -C <REPO_PATH> add <changed-files>
git -C <REPO_PATH> commit -m "fix: resolve #<NUMBER> -- <summary>"
```

Use a conventional commit message. Reference the issue number.

### 9. Report to the user

Tell the user:
> Fix committed. Review with `git -C <REPO_PATH> log -1 --stat`. Push when ready.

Do NOT push. Do NOT create PRs. the user decides when to push.

## Constraints

- Never push to remote
- Never create PRs automatically
- Use `git -C <REPO_PATH>` for all git operations (CWD stays at ~/projects/workspace/)
- Test commands get 10-minute timeout (`timeout: 600000` on Bash tool)
- If 3 test failures, escalate — do not retry a 4th time
