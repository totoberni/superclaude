---
name: good-idea
description: "Record effective solutions and patterns for reuse across sessions."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Good Idea Retrospective

Record effective solutions and patterns from this session.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project)

## Procedure

### 1. Detect Agent Class + Gather Evidence

Infer class: `o-`/`orch` → `orch`, `scaf` → `scaf`, `meta` → `meta`, `w-<type>-N` → `w-<type>`. Check if `~/.claude/agent-memory/class/<class>/mtm.md` exists (enables dual-write).

Read in parallel:
- `git -C <repo> log --oneline -20` + `git diff --stat HEAD~5..HEAD`
- Orch reports (`~/.claude/comms/*/reports.md`) — look for DONE, smooth completions
- Plan state (`~/.claude/plans/*/state*.md`) — tasks completed faster than expected

### 2. Identify + Tag Wins

Tag each win: `[WORKING_SOLUTION]` (confirmed pattern), `[DECISION]` (design choice + reasoning), `[PREFERENCE]` (style/convention).

Categories: tool usage, architecture, delegation, process, code pattern.

### 3. Scope + Dedup

| Scope | Storage | Who Writes |
|-------|---------|------------|
| Project win | `shared/projects/<project>.md` Wins table | **Meta only** |
| Cross-project | `shared/global/ltm.md` Index | Meta only |
| Class-level | `class/<class>/mtm.md` Wins table | Any agent of that class |
| Universal tool | `rules/20-tool-conventions.md` | Via promotion |

**Orchs**: `shared/projects/` is sandbox-denied. Write to class memory (primary) + instance memory (secondary). Meta promotes via `/lt-mem`.

Check for duplicates before recording. Skip if already present.

### 4. Record

**Project wins** (meta only): append `| W-<N> | <Phase> | <What Worked> | <Why> | <Reusable?> |`

If project file doesn't exist, create with standard template (# heading + Wins/Mistakes/Gotchas sections with table headers).

**Cross-project wins**: append `| CW-<N> | <Pattern> | <Source Projects> | No |` to `ltm.md`

**Class dual-write**: `| <ID> | <Pattern> | <One-liner> [<project>] | <Source> |` in `class/<class>/mtm.md`. Add `[PROMOTE]` prefix if `--universal` flag.

### 5. Check Promotion

If `Reusable? = Yes` and seen in 2+ projects:
1. Add to `shared/global/ltm.md`
2. If tool pattern → promote to `rules/20-tool-conventions.md`
3. Mark source entries as `Promoted`

### 6. Report

```
## Good Ideas Summary
| # | Win | Category | Recorded In | Reusable? | Promoted? |
|---|-----|----------|-------------|-----------|-----------|

### Patterns Promoted to Rules
- [list or "None"]
```

## Storage Paths
- **Project**: `shared/projects/<project>.md` | **Class**: `class/<class>/mtm.md` | **Global**: `shared/global/ltm.md` | **Rules**: `rules/20-tool-conventions.md`
- Write scopes: rule 12. Class writes are layer 2 only. Promotion to global via `/lt-mem`.
