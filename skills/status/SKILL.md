---
name: status
description: "Show project status from state files, git status, and plan."
model: haiku
category: orchestration
user-invocable: true
disable-model-invocation: true
allowed-tools: Read, Glob, Bash
---

# Status Dashboard

Show the current project status.

## Steps

1. Check for orchestration state in BOTH locations:
   - **In-project**: `.orchestrator/state.md` and `.orchestrator/plan.md`
   - **Superclaude**: `~/.claude/plans/*/state*.md` and `~/.claude/plans/*/plan.md`
2. If superclaude plans exist:
   - Read `~/.claude/plans/*/state*.md` for phase status (check all state files — there may be per-orch state files like `state-p1.md`)
   - Read `~/.claude/plans/*/plan.md` for current phase details
   - Count completed vs total phases
   - List pending HUMAN GATEs
   - Query broker for latest RPT per orch:
     ```bash
     DB="$HOME/.claude/comms/.broker.db"
     sqlite3 -header -column "$DB" "SELECT from_agent, seq, datetime(ts,'unixepoch') AS t, substr(body,1,80) AS preview FROM messages WHERE kind='RPT' GROUP BY from_agent HAVING ts=MAX(ts) ORDER BY ts DESC;"
     ```
3. If in-project `.orchestrator/` exists:
   - Read `.orchestrator/state.md` and `.orchestrator/plan.md`
4. Run `git status` for working tree state
5. Run `git log --oneline -5` for recent activity
6. Check project memory: `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search '<project> gotchas' -k 8` or `list --tier shared-projects` for known gotchas

## Output Format

```
## [Project Name] Status

### Phase Progress
| Phase | Name | Status |
|-------|------|--------|
| ... | ... | ... |

### Current Phase: [N — Name]
Status: [IN_PROGRESS / BLOCKED / ...]
Blockers: [none / list]

### Active Orchs
| Orch | Phase | Last Report |
|------|-------|-------------|
| ... | ... | ... |

### Git
Branch: [branch]
Uncommitted changes: [count]
Recent commits: [list]

### Pending Gates
- [ ] [gate description]

### Known Gotchas
- [from DB shared-projects tier]
```
