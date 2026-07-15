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

> **Note (updated 2026-05-09)**: Custom `subagent_type` is supported by the Agent tool (per v2.1.63+ docs at `code.claude.com/docs/en/sub-agents`). Autocommission writes the ephemeral `agents/_ephemeral/<name>.md` and the Meta/Orch invokes the Agent tool with `subagent_type: "<name>"` directly. The earlier `general-purpose`-with-embedded-prompt workaround is OBSOLETE — direct dispatch works.

**Task**: $ARGUMENTS

## When to Use

| Situation | Use |
|---|---|
| Task fits any of the 11 permanent `w-*` (matrix in `13-worker-first-mandate.md`) | The existing `w-*` (NOT this skill) |
| Task is one-off AND no existing `w-*` fits | `/autocommission` |
| Starting a permanent ork for multi-session/multi-hour work | `/handoff` (NOT this skill — DEC-001) |

`/autocommission` is the **escape hatch** for genuinely novel tasks. Default delegation should hit a permanent `w-*` ~95% of the time.

## Authority

| Agent | Can autocommission? |
|---|---|
| Meta | YES |
| Orch / Orch-* | YES |
| `w-*` (any) | NO — workers do not spawn children (wasted tokens, infinite-recursion risk) |
| Scaf | NO — infrastructure work uses normal scaf flow |

DEC-005 Q2: meta + orch only. DEC-005 Q3: cap = unlimited at this stage.

## Procedure

### Step 1 — Parse Task

Read `$ARGUMENTS`. Extract:
- One-line task summary (used for slug)
- Implied work category (mechanical, reasoning, multi-file, novel)
- Likely file scope (single, ≤3, >3, unknown)

### Step 2 — Pick Model + Effort + Thinking + Tools

Consult Per-Worker Defaults in `~/.claude/rules/13-worker-first-mandate.md`. Use the Decision Helper below as a fast lookup.

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
model: <picked>
tools: <minimum needed>
maxTurns: 30
memory: project
---
```

Body: embed the task description as instructions. To give the worker thinking depth, set `effort:` in its frontmatter, not a prompt keyword (see `13-worker-first-mandate.md` § Critical Implementation Note).

When authoring spawn prompts, keep `.workflow` / `/.deep-research` / `.ultracode` dot-escaped (see `rules/13-worker-first-mandate.md` § Trigger Escaping (Author-Time)).

### Step 5 — Spawn

Spawn the agent via Agent tool with `subagent_type: "<ephemeral-agent-name>"` (custom names from `~/.claude/agents/_ephemeral/` ARE first-class subagent types per v2.1.63+ Agent tool capability).

### Step 6 — Cleanup

Immediately after the worker returns (success OR failure), delete `~/.claude/agents/_ephemeral/<name>.md`. DEC-005 Q1: cleanup is non-optional.

```bash
rm -f "$HOME/.claude/agents/_ephemeral/<name>.md"
```

### Step 7 — Track Pattern

Upsert the autocommission pattern to the memory DB (`shared-global` tier):

```bash
# Search for existing ledger entry for this pattern:
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search "autocommission pattern <one-line-pattern>" --mode fts -k 3

# Fetch full body if found (to get current occurrence count):
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  get --name autocommission-pattern-<slug>

# Upsert (increment count in body, update last-seen date):
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  upsert --name "autocommission-pattern-<slug>" \
         --tier shared-global \
         --type reference \
         --description "R-4 autocommission pattern ledger: <one-line-pattern>" \
         --body "## Autocommission Pattern
- pattern: <one-line-pattern>
- occurrences: <N>
- first-seen: <YYYY-MM-DD>
- last-seen: <YYYY-MM-DD>
- example-task: <task description>
- ephemeral-name-template: w-eph-<slug>-<timestamp>
- recommended-model: <X>
- status: TRACKING"
```

Count prior occurrences from the DB entry. If count reaches **3**, flag promotion candidacy (R-4).

## Decision Helper

Fast lookup (coarse bands; the full per-worker matrix is the SOT in `~/.claude/rules/13-worker-first-mandate.md`): recon/lookup → **haiku · low**; standard code/doc/test/refactor → **sonnet · medium**; multi-file or architectural reasoning, planning, or `--scathingly-deep` review → **opus · high** (+ `think`/`think harder`); irreversible/frontier decisions → **opus · max** + `ultrathink`. Match the task to the closest rule-13 row for the exact defaults + escalation triggers.

If unsure, pick the next-higher tier — overspend on a one-off costs less than a wrong answer that contaminates project state.

## Output Format

```
## Autocommission Report

**Spawned**: <name>
**Model + effort**: <X> / <Y> / thinking=<Z>
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
- Permanent fleet: `~/.claude/agents/w-*.md` (current 11 agents — check FIRST before autocommissioning)
- Pattern log: memory DB `shared-global` tier — entries named `autocommission-pattern-<slug>` (search: `memory_db.py search "autocommission pattern" --mode fts -k 20`)
- Sibling skill (do not confuse): `/handoff` for permanent ork lifecycle
