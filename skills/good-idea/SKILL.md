---
name: good-idea
description: "Use when recording an effective solution or pattern for reuse"
category: memory
user-invocable: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Good Idea Retrospective

Record effective solutions and patterns from this session.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project)

## Procedure

### 1. Detect Agent Class + Gather Evidence

Infer class: `meta` → `meta`, `w-<type>-N` → `w-<type>`. Check if the class tier has entries (`mem list --tier class` filtered to your class) — enables dual-write to class tier.

Read in parallel:
- `git -C <repo> log --oneline -20` + `git -C <repo> diff --stat HEAD~5..HEAD`
- Plan state (`~/.claude/plans/*/state*.md`) — tasks completed faster than expected

### 2. Identify + Tag Wins

Tag each win: `[WORKING_SOLUTION]` (confirmed pattern), `[DECISION]` (design choice + reasoning), `[PREFERENCE]` (style/convention).

Categories: tool usage, architecture, delegation, process, code pattern.

### 3. Scope + Dedup

| Scope | DB tier | Type | Who Writes |
|-------|---------|------|------------|
| Project win | `shared` (--agent <project>) | project / feedback | **Meta only** |
| Cross-project | `global` | project | Meta only |
| Class-level | `class` (--agent <class>) | project / feedback | Any agent of that class |
| Universal tool | `rules/20-tool-conventions.md` (Edit tool) | — | Via promotion |

**Workers**: `shared` tier writes are meta only. Write to `class` tier (primary) + `instance` tier (secondary). Meta promotes via `/lt-mem`.

Search the DB for duplicates before recording: `mem search "<summary>" -k 3`. Skip or update if already present.

### 4. Record

Storage uses the v3 memory DB CLI: `mem` (on PATH; identical in Git Bash and PowerShell).

**For each win, search first, then upsert:**

```
mem search "<win summary in a few words>" -k 3
```

If a matching entry exists, reuse its `--name` slug. Otherwise derive a new kebab-case slug.

For every upsert below: first write the body to `~/.claude/tmp/<slug>.md` with the Write tool, then pass it via `--text-file`.

**Project wins** (meta only — tier=shared-projects, scoped to project; technical wins → type=project, process wins → type=feedback):

Body (`~/.claude/tmp/<slug>.md`):
```markdown
| W-<N> | <Phase> | <What Worked> | <Why> | Reusable? Yes |
```

```
mem upsert --tier shared-projects --type project --name <slug-kebab-case> --description "<one-line summary>" --agent <project-name> --text-file ~/.claude/tmp/<slug>.md
```

**Cross-project wins** (tier=shared-global, type=project or reference):

Body (`~/.claude/tmp/<slug>.md`):
```markdown
| CW-<N> | <Pattern> | <Source Projects> |
```

```
mem upsert --tier shared-global --type project --name <slug-kebab-case> --description "<one-line summary>" --text-file ~/.claude/tmp/<slug>.md
```

**Class dual-write** (tier=class, type=feedback or project; --agent=<class-name>):

Body (`~/.claude/tmp/<slug>.md`):
```markdown
| <ID> | <Pattern> | <One-liner> [<project>] | <Source> |
```

```
mem upsert --tier class --type project --name <slug-kebab-case> --description "<one-line summary>" --agent <class-name> --text-file ~/.claude/tmp/<slug>.md
```

Each `upsert` prints `upserted id=N` on success.

### 5. Check Promotion

If `Reusable? = Yes` and seen in 2+ projects:
1. Upsert to global tier: `mem upsert --tier shared-global --type project --name <cw-slug> ...` (see Step 4 Cross-project block)
2. If tool pattern → promote to `rules/20-tool-conventions.md` via Edit tool
3. Mark source entries as `Promoted`

### 6. Report

```
## Good Ideas Summary
| # | Win | Category | Recorded In | Reusable? | Promoted? |
|---|-----|----------|-------------|-----------|-----------|

### Patterns Promoted to Rules
- [list or "None"]
```

## Storage
- All writes go through `mem upsert`.
- Tier mapping: project-scoped win → `--tier shared-projects --agent <project>`; cross-project → `--tier shared-global`; class-level → `--tier class --agent <class>`; instance → `--tier instance`.
- Type mapping: technical wins → `--type project`; process/convention wins → `--type feedback`.
- Write scopes: rule 12. Class writes are layer 2 only. Promotion to global via `/lt-mem`.
- Rules promotion (`rules/20-tool-conventions.md`) still uses Edit tool directly — that file is not in the DB.

## Self-check

This skill is a ONE-SHOT retrospective producer: it records a win or pattern into memory, then finishes.

**Optional single self-check** (before the Step 4 DB write, or a Step 5 promotion): re-examine the classification once, fresh. Is the win category correct, is the tier/scope (shared vs class vs global) the right audience, is a promotion claim to a wider tier genuinely warranted rather than assumed. One pass on the classification before the write, not a review round.
