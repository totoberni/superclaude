---
name: autocommission
description: "Use when spawning an ephemeral w-* worker for a one-off task"
category: delegation
user-invocable: true
argument-hint: "<task description in quotes>"
allowed-tools: Read, Write, Bash, Glob, Grep, Agent
---

# Autocommission Ephemeral Worker

Spawn an EPHEMERAL `w-*` worker for a one-off task that no existing permanent `w-*` covers. The ephemeral agent file is auto-deleted once the worker returns (DEC-005 Q1).

> **Note (updated 2026-05-09)**: Custom `subagent_type` is supported by the Agent tool (per v2.1.63+ docs at `code.claude.com/docs/en/sub-agents`). Autocommission writes the ephemeral `agents/_ephemeral/<name>.md` and Meta invokes the Agent tool with `subagent_type: "<name>"` directly. The earlier `general-purpose`-with-embedded-prompt workaround is OBSOLETE — direct dispatch works.

**Task**: $ARGUMENTS

## When to Use

| Situation | Use |
|---|---|
| Task fits any of the 12 permanent `w-*` (matrix in `13-worker-first-mandate.md`) | The existing `w-*` (NOT this skill) |
| Task is one-off AND no existing `w-*` fits | `/autocommission` |

`/autocommission` is the **escape hatch** for genuinely novel tasks. Default delegation should hit a permanent `w-*` ~95% of the time.

## Authority

| Agent | Can autocommission? |
|---|---|
| Meta (or a plain session) | YES |
| `w-*` (any) | NO — workers do not spawn children (wasted tokens, infinite-recursion risk) |

DEC-005 Q2: meta only. DEC-005 Q3: cap = unlimited at this stage.

## Procedure

### Step 1 — Parse Task

Read `$ARGUMENTS`. Extract:
- One-line task summary (used for slug)
- Implied work category (mechanical, reasoning, multi-file, novel)
- Likely file scope (single, ≤3, >3, unknown)

### Step 2; Pick Model + Effort + Turn Cap + Tool Filter

Consult Per-Worker Defaults in `~/.claude/rules/13-worker-first-mandate.md`: take model, default effort and `maxTurns` from the closest permanent worker's row; shift effort by at most one level per § Effort Assignment. Use the Decision Helper below as a fast lookup.

### Step 3 — Generate Ephemeral Agent Name

```
w-eph-<short-task-slug>-<unix-timestamp>
```

Example: `w-eph-glob-jpegs-1715200000`. Slug ≤20 chars, kebab-case, ASCII only.

### Step 4 — Write Ephemeral Agent File

Path: `~/.claude/agents/_ephemeral/<name>.md` (NEVER `~/.claude/agents/` directly — separation prevents pollution of the permanent fleet).

Frontmatter:

```yaml
---
name: <name>
model: <closest permanent worker's model>
effort: <that worker's default, shifted at most one level>
disallowedTools: <universal deny list from ~/.claude/config/worker-tool-policy.json, plus tools that contradict the job>
maxTurns: <closest permanent worker's cap>
memory: project
---
```

No `tools:` line: the worker inherits every tool and `disallowedTools` is the negative filter (read the universal deny list from `~/.claude/config/worker-tool-policy.json`; do not copy it here). Add job-contradicting tools (for example `Edit` for a read-only job). Never add `TaskStop` to `disallowedTools`: ephemeral workers, like the permanent fleet, must be able to stop the background tasks they start.

Body: embed the task description as instructions. Depth is set by `effort:` in the frontmatter, not a prompt keyword (see `13-worker-first-mandate.md` § Critical Implementation Note).

The body ends with a `## Run hygiene` block matching the permanent workers (e.g. `agents/w-implementer.md` § Run hygiene; canonical text `skills/_shared/dispatch-contract.md` § "One job, done means stop" and § "No waiting"). Template:

```markdown
## Run hygiene

Canonical text: `skills/_shared/dispatch-contract.md` § "One job, done means stop" and § "No waiting".
- Producer: one smoke check after the final edit (at most 2 targeted runs), then checkpoint and hand back with `NEXT-CHECKS:`; no suites, reruns, sweeps or self-review. Read-only worker: report findings, no suites.
- Never poll, sleep 30 s or more, or wait on another agent's file; wrap slow commands in `timeout <N>`; jobs over 5 minutes are launched by meta (hand back the launch command).
- Stop every background task you started (TaskStop or `scpy job.py stop <id>`) before hand-back.
```

The spawn prompt also carries the dispatch-contract § 6 checkpoint path (create within the first 5 tool calls, append progressively).

When authoring spawn prompts, keep `.workflow` / `/.deep-research` / `.ultracode` dot-escaped (see `rules/13-worker-first-mandate.md` § Trigger Escaping (Author-Time)).

### Step 5 — Spawn

Spawn the agent via Agent tool with `subagent_type: "<ephemeral-agent-name>"` (custom names from `~/.claude/agents/_ephemeral/` ARE first-class subagent types per v2.1.63+ Agent tool capability).

### Step 6 — Cleanup

Immediately after the worker returns (success OR failure), delete `~/.claude/agents/_ephemeral/<name>.md`. DEC-005 Q1: cleanup is non-optional.

```
rm ~/.claude/agents/_ephemeral/<name>.md
```

### Step 7 — Track Pattern

Upsert the autocommission pattern to the memory DB (`shared-global` tier):

Search for an existing ledger entry for this pattern:

```
mem search "autocommission pattern <one-line-pattern>" --mode fts -k 3
```

Fetch the full body if found (to get the current occurrence count):

```
mem get --name autocommission-pattern-<slug>
```

Upsert (increment count in body, update last-seen date). Write this body to `~/.claude/tmp/autocommission-pattern-<slug>.md` with the Write tool:

```markdown
## Autocommission Pattern
- pattern: <one-line-pattern>
- occurrences: <N>
- first-seen: <YYYY-MM-DD>
- last-seen: <YYYY-MM-DD>
- example-task: <task description>
- ephemeral-name-template: w-eph-<slug>-<timestamp>
- recommended-model: <X>
- status: TRACKING
```

Then run (one line):

```
mem upsert --name "autocommission-pattern-<slug>" --tier shared-global --type reference --description "R-4 autocommission pattern ledger: <one-line-pattern>" --text-file ~/.claude/tmp/autocommission-pattern-<slug>.md
```

Count prior occurrences from the DB entry. If count reaches **3**, flag promotion candidacy (R-4).

## Decision Helper

Fast lookup (coarse bands; the full per-worker matrix is the SOT in `~/.claude/rules/13-worker-first-mandate.md`): recon/lookup → **haiku · low** (w-explorer, cap 40); test runs → **haiku · medium** (w-tester, cap 30); standard code/doc/refactor/review → **sonnet** at the closest row's default effort and cap; planning, merging or adversarial review → **opus · high** (w-planner, w-merger, w-hostile-reviewer). Match the task to the closest rule-13 row for the exact model, effort, range, cap and model-switch trigger.

If unsure, take the closest row and shift effort up by one level (within its range); the model stays the row's model.

## Output Format

```
## Autocommission Report

**Spawned**: <name>
**Model + effort**: <X> / <Y> / maxTurns=<Z> (closest worker: <w-name>)
**Task**: <one-line summary>
**Status**: SUCCESS | FAILURE
**Cleanup**: <name>.md deleted

### Worker Output
<verbatim or summarized worker return>

### Pattern Tracking
- Pattern: <one-line>
- Count this session: N
- Cross-session count (from DB shared-global tier): M
- PATTERN-COUNT: this is the Mth autocommission for pattern "<X>". After 3 occurrences, propose permanent w-* via R-4 protocol.   ← only if M ≥ 3
```

## Constraints

- NEVER skip cleanup (DEC-005 Q1) — the ephemeral file must be deleted whether the worker succeeded, failed, or errored.
- NEVER autocommission for a task that fits an existing permanent `w-*` — use the existing one.
- NEVER cap to fewer than 1 simultaneous ephemeral (DEC-005 Q3 = unlimited at this stage).
- NEVER write ephemeral agents to `~/.claude/agents/` directly — always `_ephemeral/` subdir.
- NEVER spawn ephemeral workers from inside a `w-*` agent — workers do not spawn children.
- ALWAYS set thinking depth via `effort:` in the ephemeral agent frontmatter, not a prompt keyword (SOT: `~/.claude/rules/13-worker-first-mandate.md` § Critical Implementation Note).
- ALWAYS log the pattern to the memory DB (`shared-global` tier) to enable R-4 promotion tracking.

## Cross-References

- Matrix SOT: `~/.claude/rules/13-worker-first-mandate.md` (Per-Worker Defaults, Autocommission Protocol Summary)
- Quality gates SOT: `~/.claude/rules/40-swarm-quality-gates.md` (R-1 schema, R-2 baseline-stash, R-3 verification, R-4 fleet expansion)
- Decisions: `~/.claude/plans/swarm-first-v2/decisions.md` (DEC-001 ephemeral-vs-permanent split, DEC-005 Q1/Q2/Q3 cleanup/authority/cap)
- Permanent fleet: `~/.claude/agents/w-*.md` (current 12 agents; check FIRST before autocommissioning)
- Pattern log: memory DB `shared-global` tier — entries named `autocommission-pattern-<slug>` (search: `mem search "autocommission pattern" --mode fts -k 20`)
