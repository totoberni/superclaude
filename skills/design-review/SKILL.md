---
name: design-review
description: "Invoke a design review of frontend changes. /design-review [PR#|branch|path]"
category: meta
user-invocable: true
argument-hint: "[PR-number | branch-name | file-path] [--loop]"
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
- Instruction: "Run all 7 phases. Use `[Blocker]/[High]/[Medium]/[Nit]` triage format. Line 1 of your return MUST be the verdict-first token `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K` (see Output Format), then the full per-phase report."

### 4. Return the review report

Display the agent's review output directly to the user. Do not summarize or filter — the full report is the deliverable.

## Output Format

Line 1 of the review return is the machine-readable token, emitted on every run so a driver can decide the round even when the tail is truncated (verdict-first, `verdict-schema.md`):

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K
```

The full 7-phase report (with `[Blocker]/[High]/[Medium]/[Nit]` findings) follows beneath the token; that report is the deliverable and is never summarised or filtered. The triage counts map onto the verdict-schema severity row:

| design-review severity | token field |
|---|---|
| `[Blocker]` | `blocking` |
| `[High]` | `major` |
| `[Medium]` | `minor` |
| `[Nit]` | `minor` |

`VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`. `round=K` is the loop round in progress (`round=1` for a one-shot review outside a loop). Only the w-design-reviewer authors the token; the conductor quotes the VERDICT line verbatim into the ledger and never rewrites it.

## Loop integration (converge)

design-review is the `frontend` reviewer in `/review-dispatch`; the 7-phase methodology above is its rubric. This section states how it plugs into the `/converge` engine; the base one-shot review behaviour above is unchanged. Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill); design-review forks to w-design-reviewer as the per-round frontend reviewer only, never the loop driver, and never seals itself.

### (a) As a reviewer (invoked each round by /converge)

`/converge` and `/review-dispatch` resolve design-review as the `frontend` reviewer (artifact-class `frontend`) and dispatch it once per round against the current diff, isolated (artefact + diff + rubric only; never the producer's reasoning or a prior clean verdict) and re-examining the CURRENT state with fresh evidence THIS round (no pre-approval, `verdict-schema.md`). Sonnet is the default reviewer model; the scope escalates to opus for cross-page consistency rather than a single view (`review-dispatch` frontend row).

Every run emits the machine token on line 1 (see Output Format), in ADDITION to the full 7-phase report, so the loop is machine-decidable. The counts map onto the verdict-schema severity row (`[Blocker]`=blocking, `[High]`=major, `[Medium]` and `[Nit]`=minor). `VERDICT: CLEAN` requires `blocking=0 major=0` (default bar); anything above emits `VERDICT: REWORK`. `round=K` is the loop round in progress (`round=1` for a one-shot review outside a loop). The conductor quotes the VERDICT line verbatim into the ledger; only design-review (through w-design-reviewer) authors the token, never the conductor or the producer under review.

### (b) --loop (conductor-driven shorthand)

**Authority: meta + orch only**, the same as `/converge`. A `w-*` worker cannot drive a loop, so `--loop` invoked from a worker is a no-op error; the base one-shot review carries no such restriction.

design-review does NOT reimplement a bespoke self-sealing loop, and never seals itself. To iterate to CLEAN, the conductor (meta/orch) runs `/converge` with artifact-class `frontend`; design-review is the round reviewer each round (emitting its VERDICT line through w-design-reviewer), and `/converge` supplies the terminal `SEAL: ACCEPTED` from a FRESH auditor of a different identity than any round reviewer (two-token protocol; design-review never seals itself). `--loop` is therefore a shorthand that prints the `/converge` frontend invocation for the conductor to run; it never self-arms (DEC-R2).

The shorthand prints this, then STOPS:

```
/converge <diff-or-artefact under review> --binding B1
```

resolved with artifact-class `frontend`, which selects design-review (w-design-reviewer) as the per-round reviewer via `/review-dispatch`. `/converge` then owns every loop mechanic (rounds, ledger, the 8 loop rules, caps, goal-string emission) and emits the `/goal` block whose clause 1 requires a `SEAL: ACCEPTED blocking=0 major=0 minor=0` (nits=0 at the gate or strict bar) quoted verbatim from a FRESH holistic auditor whose identity differs from every round reviewer, never from design-review's own `VERDICT: CLEAN`.

## Notes

- The w-design-reviewer currently works in code-only mode (no Playwright MCP)
- For git commands that need a specific repo, use `git -C <repo-path>`
- Frontend file extensions: `.tsx`, `.jsx`, `.css`, `.scss`, `.html`, `.vue`, `.svelte`

## Cross-References

- Token protocol and severity map (Blocker/High/Medium/Nit to blocking/major/minor): `~/.claude/skills/_shared/verdict-schema.md`
- Convergence engine that consumes this verdict each round: `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`frontend` class to this rubric): `~/.claude/skills/review-dispatch/SKILL.md`
