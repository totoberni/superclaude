---
name: mistake
description: "Use when recording a mistake or promoting a recurring pattern."
category: memory
user-invocable: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Mistake Retrospective

Record mistakes and patterns from this session.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project)

## Procedure

### 1. Detect Agent Class + Gather Evidence

Infer class: `meta` → `meta`, `w-<type>-N` → `w-<type>`. Check if the class tier has entries (`mem list --tier class` filtered to your class) — enables dual-write to class tier.

Read in parallel:
- `git -C <repo> log --oneline -20` + `git -C <repo> reflog --oneline -30` — look for reverts, fixups
- Plan state (`~/.claude/plans/*/state*.md`) — tasks that stalled, were retried, or ended BLOCKED

### 2. Tag Mistakes

Tag each: `[FAILURE]` (what didn't work + why), `[GOTCHA]` (counterintuitive trap), `[PATTERN]` (recurring, 2+ times).

### 3. Scope + Dedup

| Scope | DB tier | Type | Who Writes |
|-------|---------|------|------------|
| Project mistake/gotcha | `shared` (--agent <project>) | feedback / project | **Meta only** |
| Class-level | `class` (--agent <class>) | feedback | Any agent of that class |
| Universal tool pattern | `rules/20-tool-conventions.md` (Edit tool) | — | Via promotion |
| Agent operational | `instance` (--agent <agent>) | feedback | That agent |

**Workers**: `shared` tier writes are meta only. Write to `class` tier (primary) + `instance` tier (secondary). Meta promotes via `/lt-mem`.

Search the DB for duplicates before recording: `mem search "<summary>" -k 3`. If already recorded, upsert with updated body (increment Occurrences in the text).

### 4. Record

Storage uses the v3 memory DB CLI: `mem` (on PATH; identical in Git Bash and PowerShell).

**For each mistake, search first (to update an existing entry and increment its occurrence vs creating new), then upsert:**

```
mem search "<mistake summary in a few words>" -k 3
```

If a matching entry exists, reuse its `--name` slug (upsert updates in place). Otherwise derive a new kebab-case slug.

For every upsert below: first write the body to `~/.claude/tmp/<slug>.md` with the Write tool, then pass it via `--text-file`.

**Project mistakes/gotchas** (meta only — tier=shared-projects, type=feedback for process gotchas / type=project for project-specific bugs):

Body (`~/.claude/tmp/<slug>.md`):
```markdown
| M-<N> | <Phase> | <What Went Wrong> | <Root Cause> | <Fix> | <Prevention> | 1 |
```

```
mem upsert --tier shared-projects --type feedback --name <slug-kebab-case> --description "<one-line summary>" --agent <project-name> --text-file ~/.claude/tmp/<slug>.md
```

**Class dual-write** (tier=class, type=feedback; --agent=<class-name>):

Body (`~/.claude/tmp/<slug>.md`):
```markdown
| <ID> | <Summary> [<project>] | <Prevention Rule> | 1 |
```

```
mem upsert --tier class --type feedback --name <slug-kebab-case> --description "<one-line summary>" --agent <class-name> --text-file ~/.claude/tmp/<slug>.md
```

Each `upsert` prints `upserted id=N` on success.

**Universal tool patterns**: still appended to `rules/20-tool-conventions.md` via Edit tool — that file is not in the DB:
```markdown
## <Pattern Title>
- <Concise rule>
- WRONG: `<example>` | RIGHT: `<example>`
```

### 5. Check Promotion

If `Occurrences >= 2` (same pattern, different contexts):
1. Promote to `rules/20-tool-conventions.md` via Edit tool
2. Upsert a global-tier entry marking the pattern: `mem upsert --tier shared-global --type feedback --name <slug> ...`
3. Update the source DB entries to note `[PROMOTED->rules]` in the body

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

## Storage
- All mistake/gotcha writes go through `mem upsert`.
- Tier mapping: project-scoped mistake → `--tier shared-projects --agent <project>`; class-level → `--tier class --agent <class>`.
- Type mapping: process gotchas / agent behavior mistakes → `--type feedback`; project-specific bugs / codebase gotchas → `--type project`.
- Write scopes: rule 12. Class writes are layer 2 only. Promotion to global via `/lt-mem`.
- Rules promotion (`rules/20-tool-conventions.md`) still uses Edit tool directly — that file is not in the DB.

## Self-check

`mistake` is a ONE-SHOT retrospective PRODUCER: a single invocation runs the six steps above and records (or upserts) a memory entry, promoting to a rule only when Occurrences >= 2 (step 5).

Before the permanent DB write, and again before any Occ>=2 rule promotion, run ONE optional adversarial self-check on the current classification: is the mistake tagged in the right category (step 2), is the occurrence count from the dedup search (step 3) actually correct, and, for a promotion, is the prevention rule genuinely warranted rather than two unrelated failures coinciding? This is a single self-review pass, not a review loop; frame it in the no-pre-approval spirit (`_shared/verdict-schema.md`): a fresh look at the CURRENT classification taken immediately before the irreversible write, not a rubber stamp of the reasoning that produced it.
