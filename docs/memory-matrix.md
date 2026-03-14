# Memory Matrix

3-tier memory system for superclaude agents.

## Matrix Structure

```
                    Global                  Per-Project
                +-----------------------+------------------------+
  Shared        | shared/global/ltm.md  | shared/projects/*.md   |
  (Tier 1)      | 60 lines max          | 60 lines max each      |
                +-----------------------+------------------------+
  Class         | class/<class>/mtm.md  | (v3: class/projects/)  |
  (Tier 2)      | 40 lines max          | 30 lines max each      |
                +-----------------------+------------------------+
  Instance      | instance/<name>/MEMORY.md                      |
  (Tier 3)      | meta: 80 / orch: 40 / other: 30 lines max     |
                +------------------------------------------------+
```

## Tiers

| Tier | Path | Purpose | Lifespan |
|------|------|---------|----------|
| Shared | `shared/global/ltm.md` | Cross-project, cross-agent knowledge | Permanent |
| Shared | `shared/projects/<project>.md` | Project-specific knowledge for all agents | Project lifetime |
| Class | `class/<class>/mtm.md` | Agent-class patterns (orch, meta, scaf, etc.) | Medium-term |
| Instance | `instance/<name>/MEMORY.md` | Per-session recovery, agent-specific state | Session lifetime |

## Placement Rule

Place knowledge at the **most specific level that covers all consumers**:

1. Only one agent class uses it? -> `class/<class>/mtm.md`
2. Multiple classes but one project? -> `shared/projects/<project>.md`
3. All agents, all projects? -> `shared/global/ltm.md`
4. Current session only? -> `instance/<name>/MEMORY.md`

## Access Control

| Agent | shared/global | shared/projects | Own class/ | Other class/ | Own instance/ |
|-------|:---:|:---:|:---:|:---:|:---:|
| Meta | R/W | R/W | R/W | **R** | R/W |
| Scaf | R/W | R/W | R/W | **R** | R/W |
| Orch | R | R/W | R/W | -- | R/W |
| Workers | R | R | R | -- | -- |

**Orch max read set**: shared/global/ltm.md + shared/projects/<project>.md + class/orch/mtm.md + own instance MEMORY.md = **4 files** (+ project gotchas = 5 max).

## Archive Directories

Each memory cell has an `archive/` subdirectory:

- `shared/global/archive/` — archived global entries
- `class/<class>/archive/` — archived class entries

**Purpose**: tombstones for deleted/resolved entries, reference for solved problems. Entries move to archive when they're no longer actively relevant but may be useful for context on past decisions.

**Archive format**: `YYYY-MM-DD-<slug>.md` with original content + resolution note.

## System Files

```
_system/
  _compact-snapshots/    # Auto-snapshots before context compaction
  _archive/              # Legacy archive (pre-matrix)
```

## Line Budgets

| Cell | Max Lines | Action When Exceeded |
|------|-----------|---------------------|
| `shared/global/ltm.md` | 60 | Archive least-referenced entries |
| `shared/projects/<project>.md` | 60 | Archive resolved gotchas/old wins |
| `class/<class>/mtm.md` | 40 | v3 trigger: split to class/projects/ tier |
| `instance/<name>/MEMORY.md` (meta) | 80 | Compact: promote durable knowledge to class/ |
| `instance/<name>/MEMORY.md` (orch) | 40 | Compact: promote to class/ or shared/ |
| `instance/<name>/MEMORY.md` (other) | 30 | Compact: promote to class/ |

## v3 Expansion Path

When v3 triggers fire (see `shared/projects/superclaude.md`):

1. **Class mtm.md > 40 lines** -> split into `class/<class>/projects/<project>.md` (30-line budget each)
2. **Corpus > 2,000 lines** -> activate `/lt-mem` skill for automated archival
3. **3+ class cells > 30 lines** -> standalone manifest files per agent
4. **Cross-cell duplication > 10%** -> `/lt-mem` promotion logic deduplicates
