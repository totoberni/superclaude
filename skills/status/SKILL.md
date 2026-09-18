---
name: status
description: "Use when showing project status from state files, git status and plan"
model: haiku
category: orchestration
user-invocable: true
allowed-tools: Read, Glob, Bash
---

# Status Dashboard

Show the current project status.

## Steps

1. Check for orchestration state in BOTH locations:
   - **In-project**: `.orchestrator/state.md` and `.orchestrator/plan.md`
   - **Superclaude**: `~/.claude/plans/*/state*.md` and `~/.claude/plans/*/plan.md` (agents read `plan.md`; for human browser viewing, point to the rendered `plan.html` in the same dir)
2. If superclaude plans exist:
   - Read `~/.claude/plans/*/state*.md` for phase status (check all state files — there may be per-phase state files like `state-p1.md`)
   - Read `~/.claude/plans/*/plan.md` for current phase details
   - Count completed vs total phases
   - List pending HUMAN GATEs
   - Read the tail (last ~30 lines) of the spawn logs for recent worker activity: `~/.claude/comms/_spawns.log` (spawn events) and `~/.claude/comms/_outcomes.log` (worker outcomes). Either may be absent; report "(no spawn log yet)" in that case.
3. If in-project `.orchestrator/` exists:
   - Read `.orchestrator/state.md` and `.orchestrator/plan.md`
4. Run `git status` for working tree state
5. Run `git log --oneline -5` for recent activity
6. Check project memory: `mem search '<project> gotchas' -k 8` or `mem list --tier shared-projects` for known gotchas

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

### Recent Workers
| Worker | Task | Outcome |
|--------|------|---------|
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
