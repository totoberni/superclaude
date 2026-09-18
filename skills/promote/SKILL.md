---
name: promote
description: "Use when promoting a recurring autocommission pattern to a permanent w-*"
category: delegation
user-invocable: true
argument-hint: "[--threshold N] [--dry-run]"
allowed-tools: Read, Write, Bash, Grep, Glob
---

# Promote Ephemeral -> Permanent w-*

Closes R-4 (per `~/.claude/rules/40-swarm-quality-gates.md`): when an autocommission pattern recurs >= N times (default 3), it's a candidate for permanent `w-*.md` rather than continued ephemeral spawning.

**Authority**: meta only (drafts go to Meta for approval before becoming permanent agents).

**Args**: $ARGUMENTS

## Procedure

### Step 1: Read the ledger

Retrieve the autocommission patterns ledger from the DB (shared-global tier):

```
mem search "autocommission patterns occurrences" --mode fts -k 20
```

If no results: report "No autocommission patterns in DB - nothing to promote" and stop.

The ledger is maintained by `/autocommission` (Step 7 - log pattern) per its SKILL.md. Format expected:

```markdown
## Autocommission Patterns
- pattern: <task-pattern-key>
  occurrences: 3
  first-seen: 2026-04-15
  last-seen: 2026-05-08
  example-task: "rewrite LaTeX sections for clarity"
  ephemeral-name-template: w-eph-rewrite-<timestamp>
  recommended-permanent-name: w-rewriter
  recommended-model: sonnet
  recommended-effort: medium
  recommended-tools: Read, Edit, Write, Bash, Grep, Glob
```

### Step 2: Filter

Use the DB query results from Step 1, filtering by occurrence count >= threshold (default 3, override via `--threshold`).

Skip entries with `status: TRACKING`, `status: PROMOTED`, or `status: DEPRECATED` — only `READY-TO-PROMOTE` (or no status field, treated as eligible if occurrences >= threshold) should produce drafts. TRACKING entries are pre-registered patterns awaiting actual `/autocommission` events to increment their count.

Retrieve full bodies for each candidate found in Step 1 (`mem get --name <slug>`), then filter locally:
- parse the occurrences field from the body text
- skip status: TRACKING / PROMOTED / DEPRECATED
- keep only entries with occurrences >= threshold (3 unless `$ARGUMENTS` carries `--threshold <N>`)

### Step 3: Draft for each candidate

For each promotion candidate, draft a permanent `~/.claude/agents/_pending_promotion/w-<recommended-permanent-name>.md`:
- Frontmatter from `recommended-model` / `recommended-tools`
- Body: stub with sections (Mode System, Hard Rules, Output Format, Escalation) - to be filled by Meta
- Cross-reference to the DB entry (shared-global tier, pattern key)

If `--dry-run`: print drafts to stdout, do NOT write files.
Default: write drafts to `~/.claude/agents/_pending_promotion/<name>.md` with the Write tool (NOT to `agents/` directly - Meta reviews and moves). The directory is created automatically by the first write.

Draft template per candidate:

```markdown
---
name: <w-name>
description: "<derived from example-task>"
model: <recommended-model>
tools: <recommended-tools>
---

# <Agent Name>

[Stub - filled by Meta during review.]

## Mode System

[Define modes if applicable - e.g., review/fix/audit.]

## Hard Rules

[Constraints, scope limits, escalation triggers.]

## Output Format

[Expected structured output.]

## Escalation

[When to escalate to meta.]

## Provenance

Promoted from autocommission pattern (R-4 closure):
- DB entry (shared-global tier): `<pattern-key>`
- occurrences at promotion: <N>
- first-seen: <date>
- last-seen: <date>
- example task: "<example-task>"
```

### Step 4: Report

Output table:

```
| pattern | occurrences | last-seen | drafted-to | next-step |
|---------|-------------|-----------|------------|-----------|
| <key>   | N           | <date>    | <path>     | Meta review + mv to agents/ |
```

If zero candidates: output `No promotion candidates (threshold=N).`

## Authority

Meta only. Workers cannot promote (requires cross-session pattern observation).

## Constraints

- NEVER write directly to `~/.claude/agents/` - drafts go to `_pending_promotion/`
- NEVER promote without >=3 occurrences (R-4 strict criterion)
- NEVER infer `recommended-model` - must be in the DB entry body. If missing, skip the candidate and report a malformed-entry warning.
- After Meta promotes (manually `mv` from `_pending_promotion/` to `agents/`), upsert the DB entry with `promoted: true` in the body (via `/remember` or `mem upsert`).
- The `~/.claude/agents/_pending_promotion/` directory is auto-created on first promotion run; never check it into permanent agent listings.

## Post-Promotion Activation (v2.1.63+)

Once Meta moves `_pending_promotion/<name>.md` → `agents/<name>.md`, the new worker is **immediately** invokable via `Agent({subagent_type: "<name>", ...})` — no session restart needed (per `code.claude.com/docs/en/sub-agents`: "Subagents are loaded at session start; /agents interface picks up changes immediately").

## Cross-References

- R-4: `~/.claude/rules/40-swarm-quality-gates.md` § R-4
- Source ledger: written by `/autocommission` Step 7 to the DB (`shared-global` tier)
- Sister skill: `/autocommission` (writes the ledger this skill reads)
- Hierarchy + write scopes: `~/.claude/rules/12-agent-hierarchy.md`
