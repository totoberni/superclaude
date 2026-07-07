# _shared/memory-distill.md: pitfall auto-selection for briefings (SOT)

Consumed by: /rb (bootstrap "Known Pitfalls" section). Reusable by any directive/bootstrap generator needing a "Known Pitfalls" or "Watch out for" briefing section.

## Selection algorithm

From the project's memory (gotchas + mistakes, queried via `memory_db.py search '<project> gotchas mistakes' -k 8` or `list --tier shared-projects`), select the top 3-5 pitfalls by priority:

1. **Gotchas section entries** -- always relevant, always included first.
2. **Mistakes with Occurrences >= 2** -- recurring, high signal.
3. **Most recent mistakes** -- likely still relevant even at Occurrences = 1.

Stop at 5. Fewer than 3 qualify -> include what exists rather than padding with low-relevance entries.

## Output shape

Render as a numbered list under a `## Known Pitfalls` heading:

```markdown
## Known Pitfalls
1. <gotcha or mistake summary, one line, cite the source entry's slug>
2. ...
```

## Where this applies

- **Bootstraps** (`/rb`): embedded directly in the generated `bootstrap.md`.
- **Directive "Known Pitfalls" sections**: same algorithm, same 3-5 cap, when a meta/orch drafts a directive touching a project with existing memory entries.

## Cross-reference

This SELECTS existing entries for a briefing. Search-first dedup for WRITING new pitfalls is a different procedure: see [retro-evidence.md](retro-evidence.md).
