---
name: recover-truncated
description: "WORKER-TRUNCATED recovery: narrow re-dispatch + hand-off context. Meta+orch only"
category: orchestration
user-invocable: true
disable-model-invocation: true
argument-hint: "[<description-fragment>] | [--recent] [--writes]"
allowed-tools: Read, Bash, Grep, Glob
---

# /recover-truncated — WORKER-TRUNCATED Recovery Helper

Handles the recovery flow for the `WORKER-TRUNCATED` failure tag (added in V-001 rubric § 3.5). When a worker did substantial work but its output was cut off before a closing report, this skill helps the parent (meta/orch) re-dispatch a **narrower synthesizing worker** with the prior output as hand-off context.

**Critical convention**: this skill is **advisory** — it produces the re-dispatch prompt + recommended Agent payload. The caller (meta/orch) actually invokes the Agent tool with the produced prompt. Skills are not auto-executing dispatch tools per `/swarm-dispatch` and `/handoff` conventions.

---

## Usage

```
/recover-truncated <description-fragment>          # locate by fuzzy match on description
/recover-truncated --recent                        # use most recent SPAWN without matching EXIT
/recover-truncated <fragment> --writes             # promote synthesis worker to w-implementer (writes allowed)
```

Arguments parsed from `$ARGUMENTS`:
- Positional: description fragment from the truncated spawn (used for reference when filling the hand-off template in Step 4; the caller typically already knows this from their own dispatch).
- `--recent`: locate the most recently modified artifact file (subagent transcript or task output) in the current session's `subagents/` or `tasks/` directory, rather than an exact `agentId`/`task_id`.
- `--writes`: switch the recovery worker shape from read-only synthesis (w-explorer / haiku / low) to file-writing recovery (w-implementer / sonnet / medium). Default is read-only.

---

## Behavior

### Step 1: Locate the truncated worker's output and extract it

Claude Code writes each spawn's output to disk under the current session directory. Two patterns, depending on how the worker was dispatched:

```
# Foreground subagent (the common /recover-truncated case): full transcript.
~/.claude/projects/<project-slug>/<session-id>/subagents/agent-<agentId>.jsonl

# Background task (dispatched with run_in_background): final-result file,
# the `output_file` path returned to the caller at dispatch time.
~/.claude/projects/<project-slug>/<session-id>/tasks/<task-id>.output
```

If you already know the `agentId` or `task_id` (visible in your own conversation, from the spawn that just got truncated), address that file directly; this is the reliable path. Otherwise, resolve `--recent` / `<description-fragment>` against the candidates in the current session's directories:

```bash
SESSION_DIR="$HOME/.claude/projects/<project-slug>/<session-id>"

# --recent (also the default when no fragment is given): most recently
# modified artifact in the relevant directory.
AGENT_FILE=$(ls -t "$SESSION_DIR"/subagents/agent-*.jsonl 2>/dev/null | head -1)
TASK_FILE=$(ls -t "$SESSION_DIR"/tasks/*.output 2>/dev/null | head -1)
```

Then extract a compact, LLM-safe summary. Never paste raw JSONL into a prompt; both modes below hard-cap their output:

```bash
[ -n "$AGENT_FILE" ] && ~/.claude/scripts/swarm/recover-worker.sh agent "$AGENT_FILE"
[ -n "$TASK_FILE" ]  && ~/.claude/scripts/swarm/recover-worker.sh task "$TASK_FILE"
```

The printed result (last assistant text or final result, tool_use count, touched-file list) is the `<PRIOR-WORKER-OUTPUT>` content for Step 4. Combine it with what you already know about the spawn (parent, subagent_type, description, timestamp) to fill in that template.

### Step 2 — Verify state via git

Before re-dispatching, the caller MUST verify what the truncated worker actually changed (workers can leave the tree modified without saying so):

```bash
# Run from the project root the truncated worker operated in
git status --short
git diff --stat
```

The skill output reminds the caller to do this and include the result in the hand-off prompt.

### Step 3 — Pick recovery worker shape

| Mode | subagent_type | model | effort | When |
|------|---------------|-------|--------|------|
| Read-only (default) | `w-explorer` | haiku | low | Verify state + write report; no file edits needed |
| Writes (`--writes`) | `w-implementer` | sonnet | medium | Worker died mid-fix; need to complete the edit |

Both modes scope the worker to **verification + completion of the unfinished portion**, NOT redoing work.

### Step 4 — Produce hand-off prompt

The skill outputs a ready-to-paste markdown prompt. The caller pastes the **visible partial output from the truncated worker** into the `<PRIOR-WORKER-OUTPUT>` placeholder before dispatching.

Template (printed to stdout):

```markdown
## Recovery dispatch for truncated worker

**Original spawn**: <TS> | <PARENT> | <SUBAGENT> | <DESCRIPTION>

**Mode**: read-only synthesis | write-allowed completion

### Recommended Agent payload

```json
{
  "subagent_type": "<w-explorer | w-implementer>",
  "description": "Recover truncated: <first 50 chars of original description>",
  "prompt": "<see-prompt-below>"
}
```

### Spawn prompt

```
The prior worker (<SUBAGENT>) was dispatched for: "<DESCRIPTION>". Its output was TRUNCATED before producing a closing report.

VISIBLE PARTIAL OUTPUT from the truncated worker:
<PASTE-PRIOR-WORKER-OUTPUT-HERE>

YOUR SCOPE (narrow — do NOT redo work):
1. Run `git status` + `git diff --stat` to verify what is already on disk.
2. Compare against the truncated worker's stated intent (from the visible output).
3. If state is COMPLETE: write the closing report (## Verification, ## Files changed, ## Diff).
4. If state is INCOMPLETE: [read-only mode] report what is missing; [writes mode] complete the missing edit using the truncated worker's approach, then write the report.

DO NOT redo work the truncated worker already did. DO NOT expand scope. If you find inconsistencies, report them — do not silently "fix" them.

Output: markdown report with sections ## Verification, ## Files changed, ## Diff (or ## Notes if no diff).
```

### Notes
- Always paste the **truncated worker's last visible output** (even if cut mid-sentence) into the prompt — that context is essential for the recovery worker.
- If git status shows unrelated dirty paths, mention them so the recovery worker doesn't blame its predecessor.
- Use `--writes` ONLY when the truncated worker was a `w-implementer` / `w-debugger` / `w-merger` writing files. For research / recon truncation, the default read-only mode is correct.
```

---

## Notes / pitfalls

- **Skills are advisory.** This skill prints the dispatch payload but does NOT call the Agent tool itself. The caller (meta/orch) executes the dispatch, then verifies the result per R-3 (`~/.claude/rules/40-swarm-quality-gates.md`).
- **Truncation is not failure.** A truncated worker typically did 70-90% of the work — re-dispatching the **same prompt** wastes that effort. Always narrow scope to "verify + complete the missing portion".
- **Pasting prior output is the key step.** Without the prior worker's visible output, the recovery worker has no anchor and may redo work or pick the wrong file. The skill's prompt template enforces this.
- **`--writes` mode requires R-3 verification.** When you allow the recovery worker to edit files, run `git diff --stat` after it returns and confirm only expected files changed.
- **Fragment matching is fuzzy** — if `<fragment>` matches multiple spawns, the skill picks the most recent. To disambiguate, use a longer fragment.
- **Hand-off context goes in the prompt, NOT in tool input flags.** Subagents do not inherit conversation context; everything the recovery worker needs must be embedded in the spawn prompt text.

---

## Cross-References

- Failure tag: `~/.claude/plans/swarm-first-v2/validation-rubric.md` § 3.5 (`WORKER-TRUNCATED` row)
- V-001 retro: `~/.claude/plans/swarm-first-v2/validations/V-001.md` (4+ truncation events documented)
- Discipline note: `~/.claude/rules/13-worker-first-mandate.md` (worker-truncation discipline; supersedes the pre-cutover swarm-lessons memo)
- Spawn telemetry source: `~/.claude/comms/_spawns.log`
- Outcome telemetry (sister): `~/.claude/comms/_outcomes.log` (populated by `hooks/modules/42-agent-outcome.sh`)
- Sister skills: `/swarm-dispatch` (initial batch dispatch), `/autocommission` (ephemeral worker spawn)
