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
**Plan**: [absolute path to plan.md]
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
