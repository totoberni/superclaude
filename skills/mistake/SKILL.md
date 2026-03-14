---
name: mistake
description: "Record mistakes and promote recurring patterns to prevention rules."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Mistake Retrospective

Run a post-session retrospective to identify and record mistakes.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project patterns)

## Procedure

### 0. Agent Class Detection

Determine your agent class for dual-write:

1. Infer class from your agent name:
   - Names starting with `o-` or `orch` → class `orch`
   - `scaf` or `scaffolder` → class `scaf`
   - `meta` → class `meta`
   - `w-<type>` prefix → class `w-<type>` (e.g., `w-debugger-1` → `w-debugger`)
2. Check if `~/.claude/agent-memory/class/<class>/mtm.md` exists
   - **Yes**: dual-write enabled — class-applicable mistakes go here too
   - **No**: primary write only (don't create class dirs)

### 1. Gather Evidence

Read the session's work by checking (in parallel where possible):
- `git -C <repo> log --oneline -20` — recent commits (look for reverts, fixups, re-attempts)
- `git -C <repo> reflog --oneline -30` — all operations including failed merges, reset, abandoned work
- Any error output from the current conversation
- Orch reports — scan `~/.claude/comms/*/reports.md` for any orch directories that exist (look for BLOCKED status, retries)
- Orch escalations — scan `~/.claude/comms/*/escalations.md` for blocking questions that arose

### 2. Classify Learning Type

For every mistake found, assign a learning type tag:

| Type | When to Use | Example |
|------|-------------|---------|
| `[FAILURE]` | What didn't work and why | Test suite broken by import cycle |
| `[GOTCHA]` | Counterintuitive trap (environment, tooling, API) | WSL file permissions differ from Linux |
| `[PATTERN]` | Recurring anti-pattern (seen 2+ times) | Dismissing test failures as pre-existing |

Include the type as a tag prefix in the Phase column of the mistake table:

```markdown
| M-N | [FAILURE] P3 | What went wrong | Root cause | Fix | Prevention | 1 |
| M-N | [GOTCHA] P1 | Counterintuitive trap | Why it's a trap | Workaround | How to avoid | 1 |
| M-N | [PATTERN] P0 | Recurring mistake | Structural cause | Fix | Rule to add | 2 |
```

Existing entries without tags remain valid — tags are additive.

### 3. Categorize Scope

For every mistake or pattern found, classify its scope:

| Category | Scope | Storage Location |
|----------|-------|------------------|
| **Project mistake/gotcha** | One project's codebase/setup | `~/.claude/agent-memory/shared/projects/<project>.md` → Mistakes table (**meta only** — sandbox-denied for orchs) |
| **Class-level mistake** | Applies to any agent of this class | `~/.claude/agent-memory/class/<class>/mtm.md` → Mistakes table |
| **Universal tool pattern** | All agents, all projects | `~/.claude/rules/20-tool-conventions.md` |
| **Agent operational pattern** | One agent type | That agent's `~/.claude/agent-memory/<agent>/MEMORY.md` |

**Write scope**: `shared/projects/` is sandbox-denied for orchs. Orchs write mistakes to **class memory** (primary) and **instance memory** (secondary). Meta promotes to `shared/projects/` via `/lt-mem`. Orchs can still READ `shared/projects/` for existing gotchas.

### 4. Check for Duplicates

Before recording, search existing files for the same pattern:
- Read `~/.claude/rules/20-tool-conventions.md` — already a rule?
- Read `~/.claude/agent-memory/shared/projects/<project>.md` — already recorded for this project?
- Read `~/.claude/agent-memory/class/<class>/mtm.md` — already a class-level mistake?
- Read `~/.claude/agent-memory/<current-agent>/MEMORY.md` — already in agent memory?

If already recorded, increment the `Occurrences` count instead of adding a duplicate.

### 5. Record

**For project mistakes** (meta only — orchs skip this, write to class instead) — append to the Mistakes table in `~/.claude/agent-memory/shared/projects/<project>.md`:

```markdown
| M-<N> | <Phase> | <What Went Wrong> | <Root Cause> | <Fix Applied> | <Prevention Rule> | 1 |
```

If the project file doesn't exist yet, create it with the standard template:

```markdown
# <Project Name> — Agent Memory

## Wins

| # | Phase | What Worked | Why It Worked | Reusable? |
|---|-------|-------------|---------------|-----------|

## Mistakes

| # | Phase | What Went Wrong | Root Cause | Fix Applied | Prevention Rule | Occurrences |
|---|-------|----------------|------------|-------------|-----------------|-------------|

## Gotchas (project-specific)

(none yet)
```

**For project-specific gotchas** — append to the Gotchas section of the same file.

**For universal tool patterns** — append a new section to `~/.claude/rules/20-tool-conventions.md`:

```markdown
## <Pattern Title>

- <Concise description of the pattern and why it matters>
- WRONG: `<example of what fails>`
- RIGHT: `<example of what works>`
```

**For agent operational patterns** — append to the relevant section in `~/.claude/agent-memory/<agent>/MEMORY.md`. Keep it to 1-2 lines. If MEMORY.md is approaching 200 lines, create a topic file (e.g., `~/.claude/agent-memory/<agent>/patterns.md`) and link from MEMORY.md.

**Dual-write to class memory** (secondary, additive) — for each mistake, ask: "Is this specific to this project's codebase, or would any agent of my class hit this on any project?" If class-applicable AND dual-write is enabled (Step 0), append to `~/.claude/agent-memory/class/<class>/mtm.md` Mistakes table:

```markdown
| <next-ID> | <Summary> [<project>] | <Prevention Rule> | 1 |
```

- Tag with `[<project>]` to track origin
- If `--universal` flag was passed, prefix the summary with `[PROMOTE]` — signals v3 /lt-mem for promotion to global LTM
- If class dir doesn't exist, skip secondary write silently

### 6. Check Promotion Threshold

For any project mistake with `Occurrences >= 2` (same pattern in different contexts):
1. Promote it to `~/.claude/rules/20-tool-conventions.md` as a universal rule
2. Remove the inline entries from the project file
3. Add a one-liner in MEMORY.md files linking to the rule

### 7. Report

Present a summary table:

```
## Retrospective Summary

| # | Mistake | Category | Recorded In | Occurrences | Promoted? |
|---|---------|----------|-------------|-------------|-----------|
| 1 | ... | project / universal / agent | file path | N | yes/no |

### Patterns Promoted to Rules
- [list any newly promoted patterns, or "None"]

### Recommendations
- [any process improvements spotted]
```

## Memory Locations

- **Project**: `~/.claude/agent-memory/shared/projects/<project>.md` (primary)
- **Class**: `~/.claude/agent-memory/class/<class>/mtm.md` (secondary, dual-write)
- **Cross-project**: `~/.claude/agent-memory/shared/global/ltm.md`
- **Tool patterns**: `~/.claude/rules/20-tool-conventions.md` (auto-loaded)
- **Agent-specific**: `~/.claude/agent-memory/<agent>/MEMORY.md`
- Write scopes: rule 12. Class writes are layer 2 only — never write to `shared/global/ltm.md` directly. Promotion to global is handled by `/lt-mem`.
