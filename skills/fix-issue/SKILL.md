---
name: fix-issue
description: "Use when running an end-to-end fix pipeline for a GitHub issue"
category: workflow
user-invocable: true
argument-hint: "<owner/repo#number> or <issue-url>"
allowed-tools: Read, Edit, Write, Bash, Glob, Grep, Agent, Skill
---

# fix-issue: GitHub Issue Fix Pipeline

Analyse a GitHub issue, implement a fix, run tests, pass a single review, and commit.

**Arguments**: $ARGUMENTS, the issue reference (required).

## Steps

### 1. Verify GitHub CLI authentication

```bash
gh auth status
```

If not authenticated, tell the user: "Run `gh auth login` first; the GitHub CLI is not authenticated." and STOP.

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

Determine which local project under `~/code/` corresponds to `OWNER/REPO`:
- Check `gh repo view --json name -q .name` to get the repo name
- Look for a matching directory: list `~/code/` and match `<repo-name>` case-insensitively
- If no match found, tell the user and STOP

Set `REPO_PATH` to the absolute path (e.g., `~/code/<repo-dir>`).

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

Run the project's test suite covering the changed code. Use a foreground command with the tool's own timeout parameter set to 10 minutes:

```bash
# Adjust the test command to the project's stack
# Python: python -m pytest <test_file> -v
# Node: npm test -- --testPathPattern=<pattern>
# Set timeout: 600000 on the tool call (expected duration: up to 10 minutes)
```

If tests fail, fix and re-run. If tests fail 3 times, stop and report the failures to the user (per escalation rule).

### 8. Review gate (single pass)

A trivial one-line fix may skip this step. Otherwise, before committing:

1. **Test gate first** (deterministic, no LLM): the test suite from step 7 must be green on the current revision, run by you, never accepted on a producer's "tests pass" claim. A red suite means there is nothing sound to review yet.
2. **Dispatch `w-reviewer` once** on the diff (`git -C <REPO_PATH> diff`), with rubric `~/.claude/skills/code-quality/SKILL.md`. Isolation: the reviewer receives artefact + diff + rubric only, never the producer's reasoning, self-assessment, or a prior clean verdict. Use sonnet for a diff of <=3 files, opus for a larger or architectural diff.
3. **Read the `VERDICT:` line** (line 1 of the return, `VERDICT: REWORK|CLEAN blocking=N major=N minor=N`) and quote it verbatim to the user. Only the reviewer authors the token.
4. **Triage and fix**: accepted findings (blockers and majors) are fixed; contest the rest with evidence (file:line, or a re-run expected-vs-actual) and log the rebuttal. Re-run the tests after the fixes.
5. **Stop.** There is no mandated re-review; a second review pass happens only if the owner or meta judges it necessary. Open blockers that you cannot resolve are reported to the user instead of committed over.

If the build is delegated to a producer worker, follow `dispatch-contract.md`: the producer never self-certifies and returns `STATUS: DONE|PARTIAL|FAILED files=N checkpoint=<path>` as line 1. The model split applies (`dispatch-contract.md` section 5).

### 9. Commit the fix

```bash
git -C <REPO_PATH> add <changed-files>
git -C <REPO_PATH> commit -m "fix: resolve #<NUMBER> -- <summary>"
```

Use a conventional commit message. Reference the issue number.

### 10. Report to the user

Tell the user:
> Fix committed. Review with `git -C <REPO_PATH> log -1 --stat`. Push when ready.

Do NOT push. Do NOT create PRs. the user decides when to push.

## Constraints

- Never push to remote
- Never create PRs automatically
- Use `git -C <REPO_PATH>` for all git operations (CWD stays at `~/code/`)
- Test commands get a 10-minute timeout (`timeout: 600000` on the tool call)
- If 3 test failures, escalate, do not retry a 4th time
- **NEVER** author a VERDICT token as caller or producer; only the reviewer subagent emits it, the caller quotes it verbatim
- **NEVER** pass the producer's reasoning, self-assessment, or prior clean verdicts into the review dispatch (reviewer isolation)
- **NEVER** let the producer self-certify the tests; run the test gate yourself
- Commit (step 9) runs only AFTER the test gate is green and the review findings are addressed

## Cross-References

- Token protocol (VERDICT/STATUS), severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Four-part dispatch contract, model split, checkpoint-first: `~/.claude/skills/_shared/dispatch-contract.md`
- Code-review rubric read by the reviewer: `~/.claude/skills/code-quality/SKILL.md`
