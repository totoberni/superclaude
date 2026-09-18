---
name: memory-search
description: "Use when searching agent memory for a keyword or topic."
model: haiku
category: memory
user-invocable: true
argument-hint: "<search-query>"
allowed-tools: Read, Grep, Glob, Bash
---

# Memory Search

Search across all superclaude memory files for a keyword or topic. Memory is DB-resident
(v3): all tiers (instance, class, shared-projects, shared-global) are indexed in `~/.claude/agent-memory/.memory.db`.
The canonical search tool is `mem` (the `memory_db.py` CLI shim).

**Query**: $ARGUMENTS

## Procedure

### 1. Parse Query

Extract the search query from `$ARGUMENTS`. If empty, ask the user what to search for.

### 2. Query the Memory DB

```bash
mem search "<query>" -k 5 --mode hybrid
```

- Default `--mode hybrid` (semantic + keyword). Use `--mode fts` for exact jargon / IDs
  (e.g., `M-7`, hook names, exact error strings).
- Add `--json` to get machine-readable output when the caller needs structured data.
- Each result includes `id`, `name`, `tier`, `type`, `text`, and a relevance score.
- To read the full entry as rendered HTML: `mem get --name <name> --html`

### 3. Display Results

Group results by tier:

```
## Results for "<query>"

### shared-projects / project
- [workspace] "rubber-stamp diagnostic" — Occ=3, …
- [vps] "db migration gotcha" — …

### instance / meta
- "compilation loop pattern" — …

### No strong matches
```

If the hybrid search misses something, retry with `--mode fts` for exact-match recall.

### 4. If No Results

- Try a shorter or synonym query (`--mode fts` for exact jargon).
- Broaden to rules: `~/.claude/rules/*.md`
- Check skill files: `~/.claude/skills/*/SKILL.md`

### 5. For Agent Use (non-interactive)

Run with `--json`, extract the top-N `text` fields. No decoration, just facts.
