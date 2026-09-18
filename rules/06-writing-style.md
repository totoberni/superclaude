# Writing Style

Always-loaded authoring rules for text produced or edited by any agent. Applies to ALL agents, every session.

## No Em-Dashes (forward-looking, all documents)

- The em-dash (the long dash, Unicode U+2014) and the en-dash (U+2013) are forbidden in any newly authored or edited document text: prose, plans, reports, memory bodies, commit messages, LaTeX, markdown, code comments. Use semicolons, colons, commas, periods, or parentheses instead.
- Enforcement is a fix in place, not a block: on Write/Edit/MultiEdit/NotebookEdit the content-scan guard rewrites a NEW em-dash to `; ` and a new en-dash to `-` (or `; ` when spaced) before the text reaches disk, and tells you it did. The stored text then differs from your draft on those lines, so quote the stored form in any later `old_string`. Prefer not to write them in the first place.
- Plain hyphens (the ordinary "-", U+002D) are fine in compound words (read-only, win11-migration) and in markdown structure (horizontal rules, table separators, list bullets).
- Forward-looking: existing documents are grandfathered. Do NOT mass-prune pre-existing em-dashes. When editing a grandfathered doc, introduce no NEW em-dashes; purge pre-existing ones only during a full rewrite of that doc.
- Applies to ALL agents (meta, workers). Ratified by Abe 2026-07-01. Supersedes the academic-only scope of the instance/meta memory "European academic writing style".
