# Agent Manifest

Quick reference for all superclaude agents. Source of truth: the agent files themselves.

## Active Permanent Agents (13)

### Strategic (1)

| File | Tier | Default Model | Purpose | Invocation |
|------|------|---------------|---------|------------|
| `meta.md` | Strategic | opus | Cross-project supervision, plans, swarm dispatch and review. Never edits project code directly | `claude --agent meta` (via the `meta` launcher, which sets `CLAUDE_AGENT_NAME=meta`) |

A plain session with no `--agent` behaves as an ordinary Claude Code session with the rules loaded.

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
| `w-hostile-reviewer.md` | opus | One-shot adversarial methodology/technical review (effort:max); hostile-review gauntlet; returns a `VERDICT:` line first; read-only; does not loop | via Agent tool |

Aggregate distribution if fully adopted: ~5% haiku / ~70% sonnet / ~25% opus.

## Ephemeral Agents (`agents/_ephemeral/`)

`/autocommission "<task>"` writes a temporary `w-X.md` here, spawns it, then auto-cleans the file when the task completes (DEC-005 Q1: immediate cleanup). Authority: meta (or the owner's plain session); never a worker.

Currently empty between sessions — files exist only while their commission is active (the directory itself is created on first use).

## Pending Promotion Candidates (`agents/_pending_promotion/`)

When `/promote` finds an autocommission pattern that has recurred >=3 times (tracked in the `shared-global` memory tier), it drafts a permanent `w-*.md` candidate here for Meta review. Promotion to `agents/` requires explicit Meta approval per R-4 (`~/.claude/rules/40-swarm-quality-gates.md`).

## Skills (50 total)

Canonical catalogue with one-line purposes: `~/.claude/skills/README.md`.

### By Category

| Category | Count | Skills |
|----------|-------|--------|
| delegation | 4 | autocommission, promote, swarm-dispatch, swarm-status |
| orchestration | 4 | plan, pleh, recover-truncated, status |
| memory | 9 | good-idea, lt-mem, mem-health, mem-index, mem-similar, memory-prune, memory-search, mistake, remember |
| meta | 8 | code-quality+, debugging+, design-review, infra-security+, orchestrator-patterns, sanity-check, sync-upstream, test-scaffold |
| domain | 7 | experiment, experiment-harness, gas-patterns+, hpc, research, threat-model, windows-gotchas+ |
| code-quality | 2 | figure-validate, nb-monitor |
| research | 1 | hostile-review |
| testing | 1 | test-cleanup-protocol+ |
| maintenance | 1 | better-super |
| workflow | 13 | brainstorm, commit, fix-issue, git, notebook, pr, review, tdd, verify+, wf-hpc-watch, wf-hygiene, wf-nb-watch, wrap-up |

\+ = agent-only (not user-invocable)

### Agent-Only Skills

| Skill | Loaded By |
|-------|-----------|
| code-quality | w-reviewer, w-refactorer |
| debugging | w-debugger |
| gas-patterns | w-debugger |
| infra-security | w-reviewer |
| test-cleanup-protocol | w-debugger, w-tester |
| verify | w-tester |
| windows-gotchas | w-debugger |

## Hooks

One Python dispatcher per hook event: `~/.claude/hooks/dispatch.py <Event>`. Handlers live under `~/.claude/hooks/sc_hooks/handlers/` and register with `@on(event, tools, name)`; results are aggregated into a single JSON response. Design: `~/.claude/docs/guard-subsystem.md`. Registration into `settings.json`: `bin/apply-settings.ps1 -Profile superclaude`, run by the owner.

| Handler module | Event(s) | Purpose |
|----------------|----------|---------|
| `write_acl.py` | PreToolUse (write tools) | Guard: the write-ACL guard (per-role write scopes) |
| `content_scan.py` | PreToolUse (write tools, Agent) | Guard: the content-scan guard |
| `git_policy.py` | PreToolUse (shell tools) | Guard: the git-policy guard (commit/push permission, `/git`) |
| `commit_gate.py` | PreToolUse (shell tools) | Guard: the commit-gate guard (conventional commit check) |
| `git_verb.py` | PreToolUse (shell tools) | Guard: git-verb |
| `heuristics.py` | PreToolUse (Grep) | Guard: heuristics (advisory) |
| `notebook_guard.py` | PreToolUse (write + shell tools) | Guard: the notebook guard for `.ipynb` writes |
| `worker_verify.py` | PostToolUse (Agent) | The worker-verify reminder (R-3) |
| `baseline_stash.py` | PreToolUse (write tools) | The baseline-stash hook (R-2) |
| `context_check.py` | PreToolUse | The context-check hook |
| `thinking_nudge.py` | PreToolUse (Agent) | Advisory nudge on worker dispatch |
| `spawn_log.py` | PreToolUse / PostToolUse (Agent), SubagentStop | The spawn-log hooks (`~/.claude/comms/_spawns.log`, `_spawns-rich.log`, `_outcomes.log`) |
| `memory_inject.py` | SessionStart, SubagentStart | The SessionStart memory-injection hook |
| `snapshots.py` | PreCompact, Stop | The pre-compact snapshot hook, the stop snapshot hook |
| `session_cleanup.py` | SessionEnd | Per-session state cleanup |
| `latex_warn.py` | PostToolUse (write tools) | LaTeX warnings after `.tex` edits |

## Scripts

Run through the `scpy` shim (path relative to `~/.claude/scripts/`); the memory CLI has its own shim, `mem`.

| Script | Purpose |
|--------|---------|
| `memory/memory_db.py` | Memory DB CLI (`mem <sub> ...`) |
| `memory/mem_health.py` | Memory DB health score (used by `/mem-health`, `/wf-hygiene`) |
| `memory/scan_mem_matrix.py` | Shared scanner for memory matrix compliance (used by `/mem-health`, `/lt-mem`, `/memory-prune`) |
| `memory/render.py`, `memory/viewer.py` | Memory rendering and HTML viewer |
| `plan/plan_index.py`, `plan/render_plan.py` | Plan index and rendering |
| `swarm/recover_worker.py` | Truncated-worker recovery (used by `/recover-truncated`) |
| `nb-monitor.py` | Notebook run monitor (`/nb-monitor`) |
| `figure-validate.py` | Figure pre-flight validator (`/figure-validate`) |
| `experiment-harness.py` | Multi-seed runs and claim provenance (`/experiment-harness`) |
| `better_super_deps.py` | Dependency manifest tool (`/better-super`) |
| `ssot-lint.py` | SSOT drift lint (`~/.claude/docs/ssot-drift-gate.md`) |
| `settings_dupcheck.py` | Duplicate-entry check for `settings.json` |
| `statusline_telemetry.py`, `cost_cache_computer.py` | Statusline telemetry and cost cache |

## Memory Matrix

Hybrid-search SQLite DB at `~/.claude/agent-memory/.memory.db` (FTS5 + vec0). No `MEMORY.md`, `ltm.md`, or `mtm.md`. Every row carries a tier and a type.

| Tier | Scope |
|------|-------|
| `instance/<agent>` | A single agent's own memory |
| `shared-projects` | One project, all agents |
| `shared-global` | Cross-project, all agents |
| `class` | One agent class |

Types: `feedback`, `project`, `reference`, `user`. Query via `mem search|get|similar|list`; write via `/remember`, `/good-idea`, `/lt-mem`, `/mistake`. Structure detail: `~/.claude/docs/memory-matrix.md`. Access protocol: `~/.claude/rules/12-agent-hierarchy.md` § Memory Access.

## Naming Convention

- Singleton: bare name (`meta.md`)
- Workers: `w-{role}.md` (invoked via Agent tool, not `--agent`)
- Ephemeral autocommissions: `_ephemeral/w-{slug}-{ts}.md` (auto-cleaned)
- Promotion candidates: `_pending_promotion/{name}.md` (await Meta review)
