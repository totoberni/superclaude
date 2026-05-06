# Agent Manifest

Quick reference for all superclaude agents. Source of truth: the agent files themselves.

## Active Agents (12)

| File | Tier | Model | Purpose | Invocation |
|------|------|-------|---------|------------|
| `meta.md` | Strategic | opus | Cross-project supervision, plans, directives | `claude --agent meta` |
| `scaf.md` | Infrastructure | opus | ~/.claude/ infrastructure edits | `claude --agent scaf` |
| `orch.md` | Tactical (base) | opus | Project execution template | `claude --agent orch` |
| `w-reviewer.md` | Worker | opus | Read-only code review (3 modes) | via Agent tool |
| `w-debugger.md` | Worker | opus | Runtime error diagnosis + fix | via Agent tool |
| `w-refactorer.md` | Worker | opus | Targeted refactoring ops | via Agent tool |
| `w-merger.md` | Worker | opus | Git merge conflict resolution | via Agent tool |
| `w-planner.md` | Worker | opus | Plan creation/updates | via Agent tool |
| `w-design-reviewer.md` | Worker | opus | Frontend design review (7-phase) | via Agent tool |
| `o-<project>-1a.md` | Named orch | opus | <Project> | `claude --agent o-<project>-1a` |
| `o-<course-cw>-1.md` | Named orch | opus | <COURSE-CW> | `claude --agent o-<course-cw>-1` |
| `o-<project>-paper-1.md` | Named orch | opus | <PROJECT> Paper | `claude --agent o-<project>-paper-1` |

## Archived (4, in `_archive/`)

| File | Reason |
|------|--------|
| `orch-<project>-p1.md` | P1 complete |
| `orch-<project>-p2.md` | P2 complete |
| `scaf2.md` | Track B complete, merged into scaf |
| `o-<project>-1b.md` | Decommissioned |

## Skills (46 total)

### By Category

| Category | Count | Skills |
|----------|-------|--------|
| workflow | 10 | brainstorm, commit, fix-issue, pr, push, rb, review, tdd, verify, wrap-up |
| orchestration | 9 | delegate, handoff, nudge, observe, plan, pleh, portfolio, session-reaper, status |
| meta | 9 | code-quality*, debugging*, design-review, infra-security*, orchestrator-patterns*, sanity-check, sync-upstream, test-infra*, test-scaffold |
| memory | 8 | compact-mem, good-idea, mem-health, memory-prune, memory-search, mistake, remember, sanitize-mem |
| domain | 6 | experiment, gas-patterns*, hpc, research, threat-model, wsl-gotchas* |
| health | 4 | health, hook-health, skill-health, super-health |

\* = agent-only (not user-invocable)

### Agent-Only Skills (7)

| Skill | Loaded By |
|-------|-----------|
| code-quality | w-reviewer, w-refactorer |
| debugging | w-debugger |
| delegate | orch |
| gas-patterns | w-debugger |
| infra-security | w-reviewer, scaf |
| verify | orch |
| wsl-gotchas | w-debugger |

## Hooks

| Script | Event | Purpose |
|--------|-------|---------|
| `session-timer.sh` | SessionStart, PreToolUse | Dispatcher -> 8 modules |
| `pre-compact.sh` | PreCompact | Snapshot state files |
| `session-cleanup.sh` | SessionEnd | Clean timer files |

### Modules (in `hooks/modules/`)

| Module | Function |
|--------|----------|
| `00-parse.sh` | JSON input parsing |
| `05-context-check.sh` | Context estimation |
| `10-nudge.sh` | Advisory nudges |
| `20-counter.sh` | TDD edit counter |
| `25-commit-gate.sh` | Conventional commit check |
| `30-timer.sh` | Session time enforcement |
| `40-gc.sh` | PID-liveness GC |
| `50-bootstrap.sh` | Bootstrap freshness |

## Scripts (7)

| Script | Purpose |
|--------|---------|
| `infra-test.sh` | Regression suite (25 tests) |
| `test-hooks.sh` | Hook unit tests (33 tests) |
| `infra-health.sh` | Infrastructure health check |
| `session-reaper.sh` | Zombie session cleanup |
| `session-status.sh` | Session status display |
| `claude-completion.bash` | Bash completion for `--agent` |
| `generate-completions.sh` | Regenerate slash command completions |

## Memory Matrix

| Row | Cell Count | Path Pattern |
|-----|-----------|-------------|
| Shared | 1 global + 7 projects | `shared/global/ltm.md`, `shared/projects/*.md` |
| Class | 5 | `class/{meta,orch,scaf,w-debugger,w-reviewer}/mtm.md` |
| Instance | 5 | `instance/{meta,scaf,o-<project>-1a,o-<course-cw>-1,o-<project>-paper-1}/MEMORY.md` |

Root symlinks: 5 instance shortcuts + `_archive`, `_compact-snapshots`.

## Infrastructure Counts

| Category | Count |
|----------|-------|
| Agents (active) | 12 |
| Agents (archived) | 4 |
| Skills | 46 (39 user-invocable, 7 agent-only) |
| Rules | 8 |
| Hooks | 3 scripts + 8 modules |
| Scripts | 7 |
| Comms dirs | 4 active |
| Allow rules | 47 |
| Deny rules | 7 |
| Regression tests | 25 |

## Naming Convention

- Singletons: bare name (`meta.md`, `scaf.md`, `orch.md`)
- Workers: `w-{role}.md` (invoked via Agent tool, not `--agent`)
- Named orchs: `o-{project}-{seq}.md` (e.g., `o-<project>-1a`, `o-<course-cw>-1`)
- Archives: `_archive/{original-name}.md`
