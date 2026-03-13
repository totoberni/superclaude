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
