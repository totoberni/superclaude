---
name: mem-similar
description: "Use when finding memories related or near-duplicate to one."
category: memory
user-invocable: true
allowed-tools: Bash
argument-hint: "--name <slug> | --id <n> [-k N] [--tier T]"
---

# Memory Similarity

Find the corpus memories most similar to ONE existing memory. Unlike `memory-search`
(query → results), this is memory → neighbours: it ranks every other row by a hybrid of
**semantic** closeness (cosine of the precomputed bge-small embeddings) and **lexical**
overlap (token-set Jaccard of the bodies). Use it to:

- surface **near-duplicate** rows before writing a new memory (avoid fragmentation), or
- find rows that are **semantically related but worded differently** — which pure keyword
  (FTS/BM25) search misses.

The logic lives entirely in `memory_db.py similar` (the SOT); this skill just invokes it.

**Args**: $ARGUMENTS

## Procedure

### 1. Resolve the anchor

The caller names the anchor memory by `--name <slug>` or `--id <n>`. If unknown, find the
slug first with `memory-search` (or `memory_db.py search '<topic>'`), then pass its `--name`.

### 2. Run `similar`

```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  similar --name '<slug>' -k 8
```

- `--id <n>` is accepted instead of `--name`.
- `-k N` caps results (default 8).
- `--tier T` restricts candidates to one tier (e.g. `shared-projects`).
- `--json` emits the full rows (each carries `cosine`, `jaccard`, `combined`).
- Archive-tier rows are excluded unless `--tier archive` is passed.
- The anchor's PRECOMPUTED embedding is reused — the embedder is NOT reloaded.

Output columns: `combined` (0.3·jaccard + 0.7·cosine), `cos`, `jac`, name, tier, description.

### 3. Interpret

| Signal | Reading | Action |
|--------|---------|--------|
| high `cos` AND high `jac` (≳0.6/≳0.3) | likely **near-duplicate** | propose a merge for human review — never auto-merge |
| high `cos`, low `jac` | **same topic, different vocabulary** | a real relation FTS would miss; cross-link via `[[slug]]` |
| low `cos` | unrelated | ignore |

For consolidation/merge workflows this feeds, see `/lt-mem` (Step 7). Merge proposals are
ALWAYS surfaced for human review — this skill never deletes or rewrites a row.
