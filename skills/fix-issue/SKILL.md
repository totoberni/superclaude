---
name: fix-issue
description: "End-to-end GitHub issue fix pipeline. /fix-issue <owner/repo#number>"
category: workflow
user-invocable: true
argument-hint: "<owner/repo#number> or <issue-url> [--loop]"
allowed-tools: Read, Edit, Write, Bash, Glob, Grep, Agent, Skill
---

# fix-issue: GitHub Issue Fix Pipeline

Analyse a GitHub issue, implement a fix, run tests, and commit.

**Role**: a `/converge` DRIVER binding. Beyond the linear pipeline below, fix-issue runs an iterate-to-seal loop around the fix: implement, then a dual review gate, repeated until a FRESH `w-reviewer` seals the final diff. `/converge` owns every loop mechanic (rounds, ledger, the 8 loop rules, goal-string emission); this binding fixes the artefact class (code), the producer protocol, and the reviewer identity (`w-reviewer`). Read `/converge` first; the loop deltas live in the Loop integration section below.

**Authority (loop mode)**: the review gate spawns a reviewer, so multi-round loop mode requires meta or orch authority (worker spawn capability), same as `/converge`. A trivial single-pass fix (see below) needs none.

**Arguments**: $ARGUMENTS, the issue reference (required). Optional `--loop` forces multi-round loop mode. Loop mode is already the default for any fix that needs more than one revision; a trivial one-line fix may run the pipeline once (single-pass) and skip the loop.

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

## Loop integration (converge)

In loop mode, steps 6-7 (implement, run tests) plus a review gate become one `/converge` loop body: the fix iterates through produce-then-review rounds until a FRESH `w-reviewer` seals the final diff. Only then does the pipeline continue to step 8 (commit). The linear steps above are unchanged; this section wraps them. Loop mode is the default for any non-trivial fix; a trivial one-line fix may run the steps once (single-pass) and skip the loop entirely. Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill); fix-issue is invoked there as the driver, so its allowed-tools include Agent and Skill for that single driving invocation.

Each round runs the five `/converge` steps in order (see `/converge`, Iteration protocol):

1. **PRODUCE / REVISE**: implement the fix (round 1, step 6) or work the prior round's punch list (later rounds). Delegate the build to a producer worker per `dispatch-contract.md`; the producer never self-certifies and returns `STATUS: DONE|PARTIAL|FAILED files=N checkpoint=<path>` as line 1. The model split applies (`dispatch-contract.md` section 5): expected <=20 tool calls keeps sonnet-class defaults; beyond ~20-24 the dispatch overrides to `model: opus`.
2. **PERSIST**: the producer writes the changed files to disk, then the conductor appends a round-ledger entry (round, delta = files touched, open-findings count) BEFORE any review runs (`/converge`, Round ledger). Checkpoint-first: load-bearing state lives on disk, not in the final message.
3. **REVIEW (dual gate)**: both legs are verified by the conductor, never on the producer's say-so (`/converge` loop rule c, tool-verified critique). Both must be clean for the round to pass.
   - (a) **Test gate** (deterministic, no LLM, same class as `figure-validate`): the conductor runs the project test suite itself (step 7's command, `timeout: 600000`) and reads the exit code. Run this leg FIRST; a red suite fails the round and skips the reviewer dispatch outright, since there is nothing sound to review yet. Never accept the producer's "tests pass" claim as evidence.
   - (b) **Code-review gate**: resolve via `/review-dispatch <artifact-class> <diff>`, where `<artifact-class>` is `code-small` (<=3 files changed) or `code-large` (>3 files or architectural). That spawns `w-reviewer` (sonnet or opus by size, rubric `skills/code-quality/SKILL.md`, isolation: artefact + diff + rubric only). It returns `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K`.
4. **REPORT**: the conductor quotes the reviewer's VERDICT line verbatim into the transcript and the round ledger, beside the test gate's pass/fail for the SAME revision. Only reviewers author tokens; the conductor relays them.
5. **TRIAGE**: accepted findings (failing tests, review blockers and majors) become the next round's punch list; contest the rest with evidence (file:line, or a re-run expected-vs-actual). Contested findings are logged with a rebuttal.

**ESCALATE arm**: the existing 3-failure escalation (step 7) is the loop's stall guard. Stop and report to the user, and do NOT retry a 4th time, when ANY holds: the test gate fails 3 times; total findings do not decrease across 2 consecutive rounds (stall or oscillation); or rounds exceed the cap (default 4). This is the `00-universal.md` escalation-on-repeated-failure rule expressed as the loop's ESCALATE arm.

**Commit is post-seal**: step 8 (commit) runs ONLY after the loop seals, that is, a FRESH `w-reviewer` `SEAL: ACCEPTED` over the complete final diff together with the producer's separate `STATUS: DONE`. A clean SEAL over a diff whose tests are red is not a seal; green tests with open review blockers are not a seal either.

## Emitted /goal block

Loop setup ENDS by printing the ready-to-paste `/goal` block below in the canonical shape (`verdict-schema.md`, Canonical emitted /goal block), then STOPS. fix-issue never arms `/goal` itself (DEC-R2); the human pastes it to arm the independent engine, which enforces the exit conditions. `/goal` takes a natural-language CONDITION; never invent a `/goal seal ...` subcommand.

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH w-reviewer return over the final diff, is the MOST RECENT such line, and post-dates the last change to the fix, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); (2) the producer has separately stated completion (STATUS: DONE); (3) the project test suite passes on the latest revision, verified by the conductor. If review rounds exceed 4 (the default cap, or --rounds N on the underlying /converge), or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Print the block, then stop (DEC-R2); never self-arm. Only after the engine confirms the seal does step 8 (commit) run.

## Constraints

- Never push to remote
- Never create PRs automatically
- Use `git -C <REPO_PATH>` for all git operations (CWD stays at ~/projects/workspace/)
- Test commands get 10-minute timeout (`timeout: 600000` on Bash tool)
- If 3 test failures, escalate, do not retry a 4th time
- **NEVER** arm `/goal` or `/loop` yourself; print the block and stop (DEC-R2, inherited from `/converge`)
- **NEVER** author a VERDICT or SEAL token as conductor or producer; only reviewer subagents emit them, the conductor quotes verbatim
- **NEVER** reuse a round reviewer as the seal auditor; the seal is always a FRESH holistic `w-reviewer` over the complete final diff
- **NEVER** pass the producer's reasoning, self-assessment, or prior clean verdicts into the review dispatch (reviewer isolation)
- **NEVER** let the producer self-certify the tests; the conductor runs the test gate itself
- Commit (step 8) runs only AFTER the loop seals; loop mode requires meta or orch spawn authority

## Cross-References

- Engine mechanics, the 8 loop rules, round ledger, goal-string emission, DEC-R2: `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`code-small`/`code-large` to `w-reviewer`, model, rubric, isolation): `~/.claude/skills/review-dispatch/SKILL.md`
- Token protocol (VERDICT/SEAL/STATUS), bar levels, canonical /goal shape, severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Four-part dispatch contract, model split, checkpoint-first: `~/.claude/skills/_shared/dispatch-contract.md`
- Code-review rubric read by the reviewer: `~/.claude/skills/code-quality/SKILL.md`
