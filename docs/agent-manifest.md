# Agent Manifest

Quick reference for all superclaude agents. Source of truth: the agent files themselves.

## Active Permanent Agents (14)

### Strategic / Infrastructure / Tactical (3)

| File | Tier | Default Model | Purpose | Invocation |
|------|------|---------------|---------|------------|
| `meta.md` | Strategic | opus | Cross-project supervision, plans, directives, swarm orchestration | `claude --agent meta` |
| `scaf.md` | Infrastructure | opus | `~/.claude/` infrastructure edits | `claude --agent scaf` |
| `orch.md` | Tactical (base, EXCEPTION path) | opus | Persistent project execution template | `claude --agent orch` |

### Worker Fleet (12 permanent w-*)

Default models follow the SOT matrix in `~/.claude/rules/13-worker-first-mandate.md` § Per-Worker Defaults.

| File | Default Model | Purpose | Invocation |
|------|---------------|---------|------------|
| `w-explorer.md` | haiku | Read-only file recon, grep, "where is X defined" — reports `file:line` | via Agent tool |
| `w-committer.md` | haiku | Atomic git ops only (stage + conventional commit + commit) | via Agent tool |
| `w-implementer.md` | sonnet | Writes new code from a clear spec | via Agent tool |
| `w-doc.md` | sonnet | Authors prose: LaTeX sections, Markdown docs, READMEs, plans | via Agent tool |
| `w-tester.md` | sonnet | Runs test suites; classifies failures; proposes routing (read-only on code) | via Agent tool |
| `w-debugger.md` | sonnet | Diagnoses + fixes runtime errors; checks gotchas first | via Agent tool |
| `w-merger.md` | sonnet | Resolves git merge conflicts | via Agent tool |
| `w-refactorer.md` | sonnet | Targeted refactoring (extract / rename / inline / simplify) | via Agent tool |
| `w-reviewer.md` | sonnet | Read-only code review (3 modes); `--scathingly-deep` -> opus | via Agent tool |
| `w-design-reviewer.md` | sonnet | Multi-phase frontend design review (interaction/responsive/polish/a11y) | via Agent tool |
| `w-planner.md` | opus | Plan creation/updates (superclaude `plans/` or in-project `.orchestrator/`) | via Agent tool |
| `w-hostile-reviewer.md` | opus | Adversarial methodology/technical review (effort:max); hostile-review gauntlet; verdict-first seal; read-only | via Agent tool |

Aggregate distribution if fully adopted: ~5% haiku / ~70% sonnet / ~25% opus.

## Ephemeral Agents (`agents/_ephemeral/`)

`/autocommission "<task>"` writes a temporary `w-X.md` here, spawns it, then auto-cleans the file when the task completes (DEC-005 Q1: immediate cleanup). Authority: meta + orch only.

Currently empty between sessions — files exist only while their commission is active.

## Pending Promotion Candidates (`agents/_pending_promotion/`)

When `/promote` finds an autocommission pattern that has recurred >=3 times (tracked in the `shared-global` memory tier), it drafts a permanent `w-*.md` candidate here for Meta review. Promotion to `agents/` requires explicit Meta approval per R-4 (`~/.claude/rules/40-swarm-quality-gates.md`).

## Archived (`agents/_archive/`)

| File | Reason |
|------|--------|
| `o-example-5.md`, `o-example-6.md`, `o-example-7.md` | IQC orch instances retired after their phases completed |
| (legacy entries) | Earlier `orch-*.md`, `scaf2.md`, `o-<project>-1b.md` retained for reference |

## Skills (72 total)

### By Category

| Category | Count | Skills |
|----------|-------|--------|
| delegation | 5 | autocommission, promote, swarm-dispatch, swarm-status, topology-producer-reviewer |
| orchestration | 7 | delegate, handoff, nudge, plan, pleh, portfolio, session-reaper, status |
| memory | 8 | good-idea, lt-mem, mem-health, mem-index, memory-prune, memory-search, mistake, remember |
| meta | 9 | code-quality+, debugging+, design-review, infra-security+, orchestrator-patterns+, sanity-check, sync-upstream, test-infra+, test-scaffold |
| health | 4 | health, hook-health, skill-health, super-health |
| domain | 6 | experiment, gas-patterns+, hpc, research, threat-model, wsl-gotchas+ |
| testing | 1 | test-cleanup-protocol |
| workflow | 12 | brainstorm, commit, fix-issue, notebook, pr, push, rb, review, tdd, verify, wrap-up |

\+ = agent-only (not user-invocable)

### Agent-Only Skills

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
| `session-timer.sh` | SessionStart, PreToolUse | Dispatcher -> 11 modules |
| `pre-compact.sh` | PreCompact | Snapshot state files |
| `session-cleanup.sh` | SessionEnd | Clean timer files |
| `stop.sh` | Stop | Stop-event handling |
| `subagent-stop.sh` | SubagentStop | Subagent completion handling |
| `comms-schema-lint.sh` | PreToolUse (writes to comms) | Schema validation for comms messages |
| `hcom-pre-tool-use.sh` | PreToolUse | HCOM Phase A — message broker integration |
| `hcom-session-end.sh` | SessionEnd | HCOM Phase A — broker cleanup |
| `lib.sh` | (sourced) | Shared helper functions |

### Modules (in `hooks/modules/`)

| Module | Function |
|--------|----------|
| `00-parse.sh` | JSON input parsing |
| `05-context-check.sh` | Context estimation |
| `10-nudge.sh` | Advisory nudges |
| `15-baseline-stash.sh` | R-2: auto-stash baseline on `/commit false` repos |
| `20-counter.sh` | TDD edit counter |
| `25-commit-gate.sh` | Conventional commit check |
| `30-notebook-guard.sh` | Notebook safety guard for `.ipynb` writes |
| `30-timer.sh` | Session time enforcement |
| `40-gc.sh` | PID-liveness GC |
| `45-spawn-log.sh` | Subagent spawn logging for `/swarm-status` |
| `50-bootstrap.sh` | Bootstrap freshness |

## Scripts

| Script | Purpose |
|--------|---------|
| `infra-test.sh` | Regression suite |
| `test-hooks.sh` | Hook unit tests |
| `infra-health.sh` | Infrastructure health check |
| `session-reaper.sh` | Zombie session cleanup |
| `session-status.sh` | Session status display |
| `auto-archive-stale-orchs.sh` | Auto-archive orchs idle past threshold |
| `scan-mem-matrix.sh` | Shared scanner for memory matrix LOC + budget compliance (used by `/mem-health`, `/lt-mem`, `/memory-prune`) |
| `claude-completion.bash` | Bash completion for `--agent` |
| `generate-completions.sh` | Regenerate slash command completions |
| `hcom-broker.py` | HCOM Phase A — SQLite-backed message broker |
| `hcom-init.sh` | HCOM Phase A — broker initialisation |
| `hcom-status` | HCOM Phase A — broker status display |

## Memory Matrix

Hybrid-search SQLite DB at `~/.claude/agent-memory/.memory.db` (FTS5 + vec0). No `MEMORY.md`, `ltm.md`, or `mtm.md`. Every row carries a tier and a type.

| Tier | Scope |
|------|-------|
| `instance/<agent>` | A single agent's own memory |
| `shared-projects` | One project, all agents |
| `shared-global` | Cross-project, all agents |
| `class` | One agent class |

Types: `feedback`, `project`, `reference`, `user`. Query via `memory_db.py search|get|similar|list` (or `~/.claude/bin/mem`); write via `/remember`, `/good-idea`, `/lt-mem`, `/mistake`. Structure detail: `~/.claude/docs/memory-matrix.md`. Access protocol: `~/.claude/rules/12-agent-hierarchy.md` § Memory Access.

## Naming Convention

- Singletons: bare name (`meta.md`, `scaf.md`, `orch.md`)
- Workers: `w-{role}.md` (invoked via Agent tool, not `--agent`)
- Named orchs: `o-{project}-{seq}.md` (e.g., `o-example-mlmodel-3`, `o-example-2`)
- Ephemeral autocommissions: `_ephemeral/w-{slug}-{ts}.md` (auto-cleaned)
- Promotion candidates: `_pending_promotion/{name}.md` (await Meta review)
- Archives: `_archive/{original-name}.md`
