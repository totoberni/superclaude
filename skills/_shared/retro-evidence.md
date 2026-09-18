# _shared/retro-evidence.md: agent-class detection + evidence gathering + search-first dedup (SOT)

Consumed by: /good-idea, /mistake (Step 1 "Detect Agent Class + Gather Evidence", near-verbatim), /remember (--save mode's search-first-then-upsert pattern).

## Step 1: Detect Agent Class + Gather Evidence

Infer class from the caller's identity: `meta` -> `meta`, `w-<type>-N` -> `w-<type>`. Check whether the class tier already has entries (`mem list --tier class` filtered to your class) -- if so, dual-write to class tier too.

Read in parallel, varying the query by what the retrospective hunts for:

| Consumer | git query |
|---|---|
| /good-idea (wins) | `log --oneline -20` + `diff --stat HEAD~5..HEAD` |
| /mistake (failures) | `log --oneline -20` + `reflog --oneline -30` (reverts, fixups) |

Also read plan state (`~/.claude/plans/*/state*.md`): tasks that finished faster than estimate (wins) or stalled (mistakes).

## Search-first-then-upsert (dedup pattern)

Before writing ANY entry, search first to find a matching entry to update rather than duplicating:

```
mem search "<summary in a few words>" -k 3
```

- Match found: reuse its `--name` slug. `upsert` updates in place -- a stable `--name` prevents near-duplicate snapshots from piling up.
- /mistake specifically: increment `Occurrences` in the body text rather than treating a repeat as a new entry.
- /remember `--save`: runs this same search-first step against the stable slug `meta-recovery-context`, so repeated saves overwrite rather than accumulate.
- No match: derive a new kebab-case slug.

## Cross-references

- Tier/scope rules (who writes shared vs class vs instance) differ per consumer -- see each skill's own "Scope + Dedup" section; not generalized here.
- The `mem` shim is the one constant across all three consumers' storage calls. Bodies are always authored as a file under `~/.claude/tmp/` with the Write tool and passed via `--text-file`.
