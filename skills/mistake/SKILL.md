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

Record mistakes and patterns from this session.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project)

## Procedure

### 1. Detect Agent Class + Gather Evidence

Infer class: `o-`/`orch` → `orch`, `scaf` → `scaf`, `meta` → `meta`, `w-<type>-N` → `w-<type>`. Check if `~/.claude/agent-memory/class/<class>/mtm.md` exists (enables dual-write).

Read in parallel:
- `git -C <repo> log --oneline -20` + `git reflog --oneline -30` — look for reverts, fixups
- Orch reports (`~/.claude/comms/*/reports.md`) — look for BLOCKED, retries
- Orch escalations (`~/.claude/comms/*/escalations.md`)

### 2. Tag Mistakes

Tag each: `[FAILURE]` (what didn't work + why), `[GOTCHA]` (counterintuitive trap), `[PATTERN]` (recurring, 2+ times).

### 3. Scope + Dedup

| Scope | Storage | Who Writes |
|-------|---------|------------|
| Project mistake/gotcha | `shared/projects/<project>.md` Mistakes/Gotchas | **Meta only** |
| Class-level | `class/<class>/mtm.md` Mistakes table | Any agent of that class |
| Universal tool pattern | `rules/20-tool-conventions.md` | Via promotion |
| Agent operational | `instance/<agent>/MEMORY.md` | That agent |

**Orchs**: `shared/projects/` is sandbox-denied. Write to class memory (primary) + instance memory (secondary). Meta promotes via `/lt-mem`.

Check for duplicates. If already recorded, increment `Occurrences` count.

### 4. Record

**Project mistakes** (meta only): append `| M-<N> | <Phase> | <What Went Wrong> | <Root Cause> | <Fix> | <Prevention> | 1 |`

If project file doesn't exist, create with standard template (# heading + Wins/Mistakes/Gotchas sections).

**Project gotchas**: append `- **keyword**: description` to Gotchas section.

**Universal tool patterns**: append to `rules/20-tool-conventions.md`:
```markdown
## <Pattern Title>
- <Concise rule>
- WRONG: `<example>` | RIGHT: `<example>`
```

**Class dual-write**: `| <ID> | <Summary> [<project>] | <Prevention Rule> | 1 |` in `class/<class>/mtm.md`. Add `[PROMOTE]` if `--universal`.

### 5. Check Promotion

If `Occurrences >= 2` (same pattern, different contexts):
1. Promote to `rules/20-tool-conventions.md`
2. Remove inline entries from project file
3. Link from MEMORY.md

### 6. Report

```
## Retrospective Summary
| # | Mistake | Category | Recorded In | Occ | Promoted? |
|---|---------|----------|-------------|-----|-----------|

### Patterns Promoted to Rules
- [list or "None"]
### Recommendations
- [process improvements]
```

## Storage Paths
- **Project**: `shared/projects/<project>.md` | **Class**: `class/<class>/mtm.md` | **Global**: `shared/global/ltm.md` | **Rules**: `rules/20-tool-conventions.md`
- Write scopes: rule 12. Class writes are layer 2 only. Promotion to global via `/lt-mem`.
