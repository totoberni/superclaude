---
name: handoff
description: "Orch lifecycle: commission, check-in, parallelize, or session handoff"
category: orchestration
user-invocable: true
disable-model-invocation: true
argument-hint: "[--commission <project> [name]] [--continue] [--parallelize <plan>] [<target>]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /handoff — Orch Lifecycle Manager

Parse `$ARGUMENTS` to determine mode:
- `--commission <project> [name]` → Commission Mode
- `--continue` → Check-in Mode
- `--parallelize <plan-path>` → Parallelize Mode
- `<target>` or empty → Session Handoff (default)

---

## Mode 1: Commission (`--commission <project> [name]`)

### Step 1 — Survey
1. Parse pending work from `~/.claude/plans/<project>/plan.md` or inline task list
2. Read `~/.claude/comms/meta-registry.md` for existing orchs on this project
3. Read `~/.claude/agent-memory/shared/projects/<project>.md` for pitfalls

### Step 2 — Decide (DEC-NNN)
Evaluate each work item against existing orchs:

| Condition | Action |
|-----------|--------|
| Existing orch idle + scope fits | Write new DIR to existing orch's `directives.md` |
| Existing orch active, scope doesn't cover | Scaffold new orch for uncovered scope |
| No existing orch | Scaffold new orch(s) |
| Mixed | New DIRs for existing + scaffold for remainder |

Record decision as DEC-NNN (rationale + alternatives considered).

### Step 3 — Scaffold New Orchs
For each new orch, create **all 6 artifacts** (missing any = broken orch):

| # | Artifact | Location | Key Detail |
|---|----------|----------|------------|
| 1 | Agent alias | `~/.claude/agents/<name>.md` | Use template from `~/.claude/docs/usage-guide.md`. Name: `o-<project>-<seq>`. Verify stem unique via `ls ~/.claude/agents/*.md` |
| 2 | Comms dir | `~/.claude/comms/<name>/` | Must have all 4 files: `{directives,bootstrap,reports,escalations}.md` |
| 3 | DIR-001 | `comms/<name>/directives.md` | Standard format (`~/.claude/comms/README.md`). Include `### Known Pitfalls` (3-7 items from project memory) |
| 4 | Bootstrap | `comms/<name>/bootstrap.md` | Identity, comms paths, env setup, top 3 pitfalls inline, plan/state refs |
| 5 | Registry | `~/.claude/comms/meta-registry.md` | Append to Active table |
| 6 | State stub | `~/.claude/plans/<project>/state-<name>.md` | Phase headers matching plan |

Tell the user: "Start `claude --agent <name>` in a new terminal."

---

## Mode 2: Check-in (`--continue`)

Assess health of all commissioned orchs.

### Step 1 — Identify
Read `~/.claude/comms/meta-registry.md` → list all Active orchs.

### Step 2 — Assess Each Orch
For each active orch, read (in parallel where possible):
- `~/.claude/comms/<name>/reports.md` (latest RPT)
- `~/.claude/comms/<name>/escalations.md` (unanswered ESCs)
- State file (`~/.claude/plans/*/state-<name>.md`)

Classify health:

| Signal | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Last RPT | DONE / IN_PROGRESS | Same task >2 RPTs | BLOCKED |
| Escalations | None pending | ESC <30 min old | ESC >30 min unanswered |
| Commits | Regular | None in >20 min | None in session |

### Step 3 — Act
| Status | Action |
|--------|--------|
| Healthy + in-progress | Report "on track" to the user |
| Warning | Write corrective DIR with feedback from RPT analysis |
| Critical / BLOCKED | Flag to the user with diagnosis + recommended action |
| DONE | Flag for review. Include decommission instructions: move registry entry to Archive, optionally archive comms |
| Queued DIRs exist | Update with curated meta feedback based on latest RPTs |

---

## Mode 3: Parallelize (`--parallelize <plan-path>`)

Compute max-parallel orch strategy for a set of directives.

### Step 1 — Extract Scopes
Read `<plan-path>`. For each directive/task, extract the file scope (directories/files it touches).

### Step 2 — Conflict Graph + Batching
Build a conflict graph (nodes=directives, edges=overlapping file scope). Compute max independent sets via greedy largest-scope-first: pick largest unassigned → add to batch → remove conflicts → repeat → next batch.

### Step 3 — Same-Repo Safety
For same-batch, same-repo directives: **REQUIRED** worktree setup (`git worktree add`) — parallel orchs without worktrees cause checkout races (M-001, 3x). Assign non-overlapping file scopes. Include `git worktree remove` in cleanup.

### Step 4 — Output
Generate table: `| Batch | Orch | DIRs | File Scope | Constraint |`
Create/update all directives with scope constraints. Output ready for meta to copy into comms.

---

## Default: Session Handoff (`<target>` or no args)

Create a structured handoff for: `$ARGUMENTS`

1. Summarize: objective, completed items, remaining items
2. List files modified: `git diff --name-only`
3. List key decisions made + blockers/open questions
4. Write to appropriate location:
   - **Superclaude orch target**: `~/.claude/comms/<target>/bootstrap.md`
   - **In-project**: `.orchestrator/handoff-$(date +%Y%m%d-%H%M).md`
   - **Session-to-session** (no target): present in chat

### Template
Include: completed items, remaining items, files modified (`git diff --name-only`), key decisions, blockers, and pointers to project memory / plan / state files.

### Memory Matrix Context

When creating a handoff, include the relevant memory load order for the receiving agent:

```
## Memory Context
1. `~/.claude/agent-memory/instance/<agent>/MEMORY.md`
2. `~/.claude/agent-memory/shared/projects/<project>.md`
3. `~/.claude/agent-memory/class/<class>/mtm.md`
4. `~/.claude/agent-memory/shared/global/ltm.md`
```

Flag any memory cells that were updated during this session (the receiver should read those first).
