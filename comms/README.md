# Comms Protocol — Meta <> Orch(s)

Communication bus between Meta (Terminal 1) and Orch instances (Terminal 2+). the user relays between terminals.

## Directory Structure

```
~/.claude/comms/
  README.md                        <- This file
  <orch-name>/                     <- One per orch instance
    directives.md                  <- Meta -> this orch (DIR-NNN index, append-only)
    directives/                    <- Optional: split directive files (large campaigns)
      DIR-NNN.md                   <- Individual directive (write-once-read-many)
    bootstrap.md                   <- Meta -> this orch (cold-start context, overwrite)
    reports.md                     <- This orch -> Meta (RPT-NNN, append-only)
    escalations.md                 <- This orch -> Meta/the user (ESC-NNN)
    parent.session                 <- Meta's session_id (written before orch launch)
```

Naming: `o-<project>-<seq>` (new) | `orch-<project>-<phase>` (legacy, symlink compat)

### Split Directives (large campaigns, 10+ directives)

For campaigns with many directives, `directives.md` serves as an **index** (execution order, session plan, status table) and individual directives live in `directives/DIR-NNN.md`. Agents read the index first, then only the directive file for their current session. This saves context (~40 lines per directive vs reading the full monolith).

## Write Scope

See `~/.claude/rules/12-agent-hierarchy.md` for the full ownership table. Summary: Meta writes directives/bootstrap, orch writes its own reports/escalations.

## Message Formats

### Directive (DIR-NNN)
```
## DIR-NNN -- [Title]
**Time**: YYYY-MM-DD HH:MM
**Project**: ...
**Repo**: [absolute path]
**Plan**: [absolute path to plan.md]  <!-- agents read plan.md (SOT); humans open plan.html (rendered view) in the same dir -->
**State**: [absolute path to state.md]
**Phase/Tasks**: ...
**Instruction**: ...
**Constraints**: ...
**Files off-limits**: ...
```

### Report (RPT-NNN)
```
## RPT-NNN -- [Title]
**Time**: YYYY-MM-DD HH:MM
**Directive**: DIR-NNN
**Status**: DONE | BLOCKED | IN_PROGRESS
**Summary**: ...
**Artifacts**: [commits, files changed]
**Next**: ...
```

### Escalation (ESC-NNN)
```
## ESC-NNN -- [Question]
**Time**: YYYY-MM-DD HH:MM
**Context**: ...
**Options**: [2-3 choices with trade-offs]
**Recommendation**: ...
**Blocking**: [what's blocked until answered]
```

### Escalation Answer (Meta appends below ESC entry)
```
### Answer (Meta, YYYY-MM-DD HH:MM)
**Decision**: ...
**Rationale**: ...
```

## Parent Session Protocol

Meta writes `parent.session` to each orch's comms directory before launching the orch. The file contains meta's session_id (a single line). This enables orchs to locate and nudge their parent meta.

**One-to-many**: each orch's comms dir has its own `parent.session`, all pointing to the same meta session.

### Fallback Chain (orch reads on startup)

1. Read `~/.claude/comms/<own-name>/parent.session` → get meta's session_id
2. Check `~/.claude/session-timers/<session_id>.pid` → verify meta PID is alive (`kill -0 $PID`)
3. If alive: nudge meta via tmux `send-keys` or file-based nudge
4. If dead: scan `~/.claude/session-timers/*.agent` for any file containing `meta`
5. If meta found: use that session's PID/pane for nudge
6. If no meta found: fall back to `/nudge 200` (report to the user directly)

### Workers

Workers spawned via the Agent tool inherit context from their spawning orch's session. No additional `parent.session` infrastructure needed — workers can read the orch's comms directory if needed.

## Comms Search Store + HTML Reports (v3)

Two comms stores coexist:

- `.broker.db` — the live HCOM **message bus** (DIR/RPT/ESC/NUDGE/EVENT). Operational unread/unanswered state lives here in the `read_at` column. **This is the operational source of truth**: how meta/orchs detect unread DIR/RPT/ESC is unchanged.
- `.comms.db` — a SEPARATE, forward-only **FTS5 + vector + HTML search index** built from the broker by `scripts/memory/comms_db.py sync` (reuses the memory-DB engine). It embeds every message and renders HTML on demand. Additive search/render layer, NOT a bus replacement.

**When to use the search store**: historical or semantic queries across all comms history (e.g. "all ESCs about X", "what did orch Y report about Z"). For unread/unanswered state, use the broker (unchanged).

`comms_db.py` CLI (run via `~/.claude/.venv/bin/python`):

- `sync [--rebuild]` — forward-only refresh from the broker. Lazy/manual: run before searching to capture recent messages.
- `search <query> [--mode hybrid|fts|vec]` — hybrid FTS5+vector over all comms history.
- `html <id>` — render one entry.
- `stats` — index stats.

`comms_viewer.py` renders a standalone HTML report:

- `--id N` — one entry.
- `--agent X [--kind RPT]` — an orch's report bundle.
- `--search Q` — search hits.
- `--demo` — embedded Mermaid/Vega/TikZ sample.

Terminal comms stay MD; HTML is for completion reports + browser review.

## Bus Identity + Flat-File Parity (ESC-002 (a+))

**Bus identity = `(from_agent, to_agent, kind, seq)`**, enforced at the DB level by the partial
UNIQUE index `idx_messages_identity` (`WHERE seq IS NOT NULL`) plus `INSERT OR IGNORE` at both
writers (`hcom-broker.py send` and `hcom_backfill.py`). NULL-seq kinds (NUDGE/EVENT) are
unconstrained by design. Semantics:

- **Flat file = full-body SOT.** The MD entry in `comms/<agent>/` is the authoritative full text.
- **Condensed direct-send = blessed pattern.** Agents may `send` a condensed body for real-time
  routing; the identity constraint guarantees a later backfill can never duplicate it.
- **Backfill = gap-filler.** `hcom-backfill.sh` only inserts identities missing from the bus;
  existing ones count as `skipped_existing`.
- **Divergence = reported, tolerated.** When a skipped entry's `md5(body)` differs from the bus
  row's, it is flagged `divergent_body` (count + per-entry line, in both dry-run and apply) —
  surfaced, never silently absorbed. Re-sends of an existing identity are no-ops (exit 0 +
  `already-on-bus` notice).
- **`backfill_audit` = pure log** of backfill-applied rows. It is NOT a dedup oracle — the bus
  itself is (the schema constraint binds every writer, present and future).
