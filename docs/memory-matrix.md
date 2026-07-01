# Memory Matrix

DB-backed memory model for superclaude agents. Persistent memory is a single hybrid-search SQLite store at `~/.claude/agent-memory/.memory.db` (FTS5 lexical + sqlite-vec vector embeddings). There is no `MEMORY.md`, `ltm.md`, or `mtm.md`, and no per-file line budgets.

This file is the STRUCTURE reference: tiers, types, placement, and re-tiering. For the ACCESS PROTOCOL (search discipline, the get-by-name resolution ladder, hybrid-search mechanics), see the canonical SOT, `~/.claude/rules/12-agent-hierarchy.md` § Memory Access. Don't restate it here; link.

## Tiers

Every row carries a tier. Choose it with the Placement Rule below.

| Tier | Scope | Holds |
|------|-------|-------|
| `instance/<agent>` | A single agent's own memory | Session recovery, agent-specific state |
| `shared-projects` | One project, all agents | Project gotchas, wins, decisions |
| `shared-global` | Cross-project, all agents | Cross-project lessons, tool patterns |
| `class` | One agent class | Agent-class patterns (orch, meta, w-debugger, etc.) |

## Types

Every row also carries a type.

| Type | Captures |
|------|----------|
| `feedback` | How to approach work: corrections and validated approaches |
| `project` | Ongoing work, goals, incidents, and decisions within a project |
| `reference` | Pointers to where information lives in external systems |
| `user` | The user's role, goals, preferences, and knowledge |

## Placement Rule

Place a memory at the most specific tier that covers all its consumers:

1. Only one agent uses it? -> `instance/<agent>`
2. One agent class? -> `class`
3. Multiple agents but one project? -> `shared-projects`
4. All agents, all projects? -> `shared-global`

## Access

- **Query**: `memory_db.py search | get | similar | list`, or the `~/.claude/bin/mem` shorthand (`mem search|get|similar|list`). Search discipline and the get-by-name resolution ladder live in rules/12 § Memory Access, not here.
- **Write**: only through the memory skills, never by hand-editing the DB or any `.md` file.

  | Skill | Role |
  |-------|------|
  | `/remember` | Save or load context, cheaper than compaction |
  | `/good-idea` | Record an effective solution or pattern for reuse |
  | `/mistake` | Record a mistake; promote recurring ones to prevention rules |
  | `/lt-mem` | Consolidate: re-tier mature entries, prune stale, merge near-dups |

- **Inspect and maintain**: `/mem-health` (score DB health /100), `/memory-prune` (advisory stale/broken scan), `/mem-similar` (find near-duplicates), `/mem-index` (browse the DB by tier/type, show stats).

## Re-tiering and Archival

Memories move in place, preserving the row and its embedding (no re-embed, no duplicate, reversible):

| Operation | Effect |
|-----------|--------|
| `memory_db.py retier --tier <t>` | Deliberately move a row to a target tier |
| `memory_db.py archive` | Move a row to the `archive` tier: excluded from default search and recall, still queryable via `--all` or `--tier archive`, reversible with `--unarchive` |
| `memory_db.py prune` | Delete a row plus its FTS and vector entries |

Archival replaces the old per-cell `archive/` subdirectories: an archived row is kept for context on past decisions, not deleted.

## System Files

```
agent-memory/
  .memory.db                    # canonical hybrid-search store (FTS5 + vec0)
  _system/_compact-snapshots/   # state snapshots taken before context compaction
  _system/_stop-snapshots/      # state snapshots taken on stop events
```

## Cross-References

- Access protocol (canonical SOT): `~/.claude/rules/12-agent-hierarchy.md` § Memory Access
- Worker memory inheritance: same section (subagents get no SessionStart slice, so they rely on proactive recall)
- Programming principles governing memory writes: `~/.claude/rules/15-programming-principles.md`
- Context lifecycle and self-compact protocol: `~/.claude/rules/25-context-management.md`
