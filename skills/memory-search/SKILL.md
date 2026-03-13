---
name: memory-search
description: "Search across all agent memory files for a keyword or topic. /memory-search <query>"
user-invocable: true
argument-hint: "<search-query>"
allowed-tools: Read, Grep, Glob, Bash
---

# Memory Search

Search across all superclaude memory files for a keyword or topic.

**Query**: $ARGUMENTS

## Procedure

### 1. Parse Query

Extract the search query from `$ARGUMENTS`. If empty, ask the user what to search for.

### 2. Search All Memory Locations

Use the Grep tool to search across these locations (run in parallel):

1. **Per-agent memories**: `~/.claude/agent-memory/*/MEMORY.md`
2. **Project memories**: `~/.claude/agent-memory/shared/projects/*.md`
3. **Compact snapshots**: `~/.claude/agent-memory/_compact-snapshots/*`
4. **Cross-project wins**: `~/.claude/agent-memory/shared/wins.md`

For each search, use:
- `output_mode: "content"` to show matching lines
- `context: 2` to show 2 lines before/after each match
- `head_limit: 20` to avoid flooding context

### 3. Display Results

Group results by source file:

```
## Results for "<query>"

### ~/.claude/agent-memory/shared/projects/example-project.md
- M-4: [FAILURE] P3 — Dismissed test failures as pre-existing...
- W-7: [WORKING_SOLUTION] P2 — Dependency-ordered merges...

### ~/.claude/agent-memory/scaffolder/MEMORY.md
- Hook arithmetic: validate with [[ "$VAR" =~ ^[0-9]+$ ]]...

### No matches in:
- agent-memory/meta/MEMORY.md
- agent-memory/_compact-snapshots/
```

### 4. If No Results

- Suggest related terms (e.g., "merge" if user searched "conflict")
- Broaden the search to rules: `~/.claude/rules/*.md`
- Check skill files: `~/.claude/skills/*/SKILL.md`

### 5. For Agent Use (non-interactive)

Return a concise summary of top 5 matches with file paths and line context. No decoration, just facts.
