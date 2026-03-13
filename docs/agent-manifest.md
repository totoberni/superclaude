# Agent Manifest

Quick reference for all superclaude agents. Source of truth: the agent files themselves.

## Active Agents

| File | Tier | Model | Purpose | Invocation |
|------|------|-------|---------|------------|
| `meta.md` | Strategic | opus | Cross-project supervision, plans, directives | `claude --agent meta` |
| `scaffolder.md` | Infrastructure | opus | ~/.claude/ infrastructure edits | `claude --agent scaffolder` |
| `orch.md` | Tactical (base) | opus | Project execution template | `claude --agent orch` |
| `w-reviewer.md` | Worker | sonnet | Read-only code review | via Agent tool |
| `w-debugger.md` | Worker | sonnet | Runtime error diagnosis + fix | via Agent tool |
| `w-refactorer.md` | Worker | sonnet | Targeted refactoring ops | via Agent tool |
| `w-merger.md` | Worker | sonnet | Git merge conflict resolution | via Agent tool |
| `w-planner.md` | Worker | opus | Plan creation/updates | via Agent tool |
| `w-design-reviewer.md` | Worker | sonnet | Frontend design review (7-phase) | via Agent tool |

## Named Orch Instances

| File | Project | Phase | Status |
|------|---------|-------|--------|
| `orch-example-project-p3.md` | EXAMPLE_PROJECT | P3 merge+tests | DIR-004 ready |
| `orch-example-project-review-p3.1.md` | EXAMPLE_PROJECT | P3.1 review | Blocked |
| `orch-example-project-review-p3.2.md` | EXAMPLE_PROJECT | P3.2 review | Blocked |

## Archived (in `_archive/`)

| File | Project | Reason |
|------|---------|--------|
| `orch-example-project-p1.md` | EXAMPLE_PROJECT | P1 complete |
| `orch-example-project-p2.md` | EXAMPLE_PROJECT | P2 complete |

## Skills (30 total)

### User-Invocable (23)

| Skill | Purpose |
|-------|---------|
| `/brainstorm` | Design-before-implementation gate |
| `/commit` | Draft conventional commit |
| `/compact-mem` | Manual context stash |
| `/design-review` | Frontend design review via w-design-reviewer |
| `/fix-issue` | End-to-end GitHub issue fix pipeline |
| `/good-idea` | Record wins to project memory (with learning type tags) |
| `/handoff` | Session transition document |
| `/health` | Infrastructure health check |
| `/memory-search` | Search across all agent memory files |
| `/mistake` | Record mistakes to project memory (with learning type tags) |
| `/nudge` | Status report trigger (self or cross-agent) |
| `/orchestrator-patterns` | Orch best practices reference |
| `/plan` | Fork into w-planner agent |
| `/pleh` | Fork read-only helper (self or cross-agent) |
| `/pr` | Create GitHub PR |
| `/push` | Toggle git push deny rules |
| `/review` | Fork into w-reviewer agent |
| `/sanitize-mem` | Clean auto-memory of stale entries |
| `/sanity-check` | Code review an orch's changes |
| `/session-reaper` | Monitor/clean claude processes |
| `/status` | Dashboard: phase progress, git state |
| `/sync-upstream` | Pull upstream reference updates |
| `/tdd` | RED-GREEN-REFACTOR TDD cycle |

### Agent-Only (7)

| Skill | Loaded By | Purpose |
|-------|-----------|---------|
| `code-quality` | w-reviewer | Code quality patterns |
| `debugging` | w-debugger | Systematic debugging methodology + references |
| `delegate` | orch | Fresh subagent per task with two-stage review |
| `gas-patterns` | w-debugger | Google Apps Script patterns |
| `infra-security` | scaffolder | Security patterns |
| `verify` | orch | Evidence-before-claims gate |
| `wsl-gotchas` | w-debugger | WSL-specific gotchas |

## Infrastructure Counts

| Category | Count |
|----------|-------|
| Agents (active) | 14 + 5 symlinks |
| Agents (archived) | 2 |
| Skills | 30 (23 user-invocable, 7 agent-only) |
| Rules | 8 |
| Hooks | 3 scripts |
| Scripts | 4 |
| Comms dirs | 6 |
| Allow rules | 47 |
| Deny rules | 7 |

## Backward Compatibility

Old worker names (`code-reviewer`, `debugger`, `merge-resolver`, `refactorer`, `planner`) are symlinked to the new `w-` files. Both old and new names work with the Agent tool's `subagent_type` field.

## Naming Convention

- Singletons: bare name (`meta.md`, `scaffolder.md`, `orch.md`)
- Workers: `w-{role}.md` (invoked via Agent tool, not `--agent`)
- Named orchs: `orch-{project}-{phase}.md`
- Archives: `_archive/{original-name}.md`
