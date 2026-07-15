# Superclaude Usage Guide

> Quick reference for `~/.claude/` infrastructure. Detailed rules auto-load every session.

---

## Architecture

```
~/.claude/
  CLAUDE.md                        # Global user instructions
  settings.json                    # Permissions, hooks, sandbox
  hooks/                           # Dispatcher + modular hooks + lib.sh
  rules/                           # Auto-loaded rules (numbered order)
  agents/                          # Specialist agents (+ _ephemeral/, _pending_promotion/, _archive/)
  comms/                           # Meta <> Orch(s) communication bus
  plans/                           # Cross-project orchestration
  agent-memory/                    # Hybrid-search memory DB (.memory.db)
  skills/                          # Slash commands and agent-loaded skills
  docs/                            # This guide + manifests + hcom-design.md
  upstream/                        # Curated external references
  scripts/                         # Utilities (health, completions, reaper, scan-mem-matrix)
```

Rules load automatically. Agents activate via Agent tool or `--agent`. Skills appear in `/` menu or get injected into agents at startup.

---

## Swarm-First Defaults (v2)

The infrastructure is now **swarm-first**: Meta+w-swarm is the default delegation pattern. Persistent orks are the **EXCEPTION**, reserved for work that genuinely benefits from multi-hour state.

### Decision Tree: Meta+Swarm vs Ork Handoff

Use **Meta + w-swarm** when ALL of:

- Estimated wall-clock <= 4 hr
- Distinct subtasks < 8 with clean independent scopes
- No persistent compile-gate <-> edit-loop coupling required
- Total context for synthesis < 1M tokens
- Single-project scope (no cross-repo coordination)

Use **Ork (handoff)** when ANY of:

- Multi-day campaign (HPC training, ACM-style multi-hour assembly)
- Persistent compile-gate <-> edit-loop coupling (LaTeX rebuild every change with iterative state)
- Multi-orch parallelism in same repo (already needs ork-tier coordination)
- Multi-session continuity required (ork preserves identity across hard-blocks)
- HW EDA pipelines (full DC synth -> gate-sim -> example-tool loop)

Full SOT: `~/.claude/rules/13-worker-first-mandate.md`. Quality gates: `~/.claude/rules/40-swarm-quality-gates.md`.

### Pre-Action Trigger

Before any task that takes >3 tool calls, ask:

> *"Is this menial enough to delegate to a `w-`? Can I focus my context on synthesis / design / decision-making instead?"*

If YES -> delegate (use `/autocommission` if no permanent `w-*` fits). If NO -> retain only when surgical edit <=50 lines or single-shot decision needing your full context.

### Subagent Thinking Depth

Thinking depth propagates via the effort chain, not prompt keywords: set `effort:` in the worker's `agent.md` or override model/effort at dispatch. Full doctrine: `rules/13-worker-first-mandate.md` § Critical Implementation Note.

---

## Agents

| Agent | Invocation | Default Model | Purpose |
|-------|------------|---------------|---------|
| `meta` | `claude --agent meta` | opus | Cross-project coordination, plans, directives, swarm orchestration |
| `scaf` | `claude --agent scaf` | opus | Superclaude infrastructure (agents, hooks, rules, settings.json) |
| `orch` / `o-<proj>-<seq>` | `claude --agent o-<name>` | opus | Persistent project execution (EXCEPTION path — see decision tree) |

### Worker Fleet (12 permanent w-*)

| Worker | Model | Use Case |
|--------|-------|----------|
| `w-explorer` | haiku | Read-only file exploration, grep, recon. Reports `file:line` references — never edits |
| `w-committer` | haiku | Atomic git ops only (stage + write conventional commit + commit). Read-only on code |
| `w-implementer` | sonnet | Writes new code from a clear spec. Distinct from refactorer/debugger |
| `w-doc` | sonnet | Authors prose: LaTeX sections, Markdown docs, READMEs, plans/specs |
| `w-tester` | sonnet | Runs project test suites; classifies failures; proposes routing (read-only on code) |
| `w-debugger` | sonnet | Diagnoses + fixes runtime errors; checks gotchas first; minimal fix; records pattern |
| `w-merger` | sonnet | Resolves git merge conflicts using both sides + commit history |
| `w-refactorer` | sonnet | Targeted refactoring: extract function, rename, inline, simplify; minimal blast radius |
| `w-reviewer` | sonnet | Read-only code review (3 modes: general/infra/security). `--scathingly-deep` -> opus |
| `w-design-reviewer` | sonnet | Multi-phase frontend design review (interaction, responsive, polish, a11y, robustness) |
| `w-planner` | opus | Creates / updates project plans (superclaude `plans/` or in-project `.orchestrator/`) |
| `w-hostile-reviewer` | opus | Adversarial methodology/technical review (effort:max); runs the hostile-review gauntlet; verdict-first seal; read-only |

Aggregate distribution if fully adopted: ~5% haiku / ~70% sonnet / ~25% opus.

### Ephemeral Workers (via `/autocommission`)

For one-off tasks where no permanent `w-*` fits, `/autocommission "<task>"` writes a temporary `w-X.md` to `~/.claude/agents/_ephemeral/`, spawns it, and auto-cleans the file when the task finishes. Authority: meta + orch only.

If an autocommissioned pattern recurs >=3 times across sessions (tracked in the `shared-global` memory tier), `/promote` drafts a permanent `w-*.md` candidate to `~/.claude/agents/_pending_promotion/` for Meta review.

### Hierarchy + Write Scopes

Full table: `~/.claude/rules/12-agent-hierarchy.md`. Worker-first mandate: `~/.claude/rules/13-worker-first-mandate.md`. Quality gates: `~/.claude/rules/40-swarm-quality-gates.md`.

### Named Orch Alias Template

For the EXCEPTION path. Create `~/.claude/agents/o-<project>-<seq>.md`:

```markdown
---
name: o-<project>-<seq>
description: "Orch instance for <project> <scope>"
tools: Read, Write, Edit, Bash, Glob, Grep, Agent
model: opus
memory: user
maxTurns: 200
---

# o-<project>-<seq>

You are **o-<project>-<seq>**, a named orchestrator instance.

- **Comms**: `~/.claude/comms/o-<project>-<seq>/`
- **Memory**: your `instance/o-<project>-<seq>` tier in the memory DB (`~/.claude/agent-memory/.memory.db`)
- **Gotchas**: the `shared-projects` tier (query `memory_db.py search "<project> gotchas"`)

**Startup**: Memory -> `~/.claude/agents/orch.md` (full protocol) -> bootstrap -> plan/state -> gotchas -> execute directive.
```

---

## Slash Commands (72 skills, grouped by purpose)

### Delegation (5)

| Skill | Purpose |
|-------|---------|
| `/handoff` | Permanent-ork lifecycle: commission, decommission, check-in, session handoff |
| `/autocommission` | Spawn ephemeral `w-*` worker for one-off task; auto-cleanup on done |
| `/swarm-dispatch` | Launch parallel `w-*` worker batch using W-1/W-4/W-7/W-11 patterns |
| `/topology-producer-reviewer` | Producer-Reviewer dyad: pair worker output with reviewer audit (FG or BG) |
| `/promote` | Scan the `shared-global` tier for >=3-occurrence autocommission patterns; draft permanent `w-*.md` |
| `/swarm-status` | Live snapshot of in-flight workers, BG reviewer queue, ephemeral agents |

### Orchestration (7)

`/delegate`, `/handoff`, `/nudge`, `/plan`, `/pleh`, `/portfolio`, `/session-reaper`, `/status`

### Memory (8)

| Skill | Purpose |
|-------|---------|
| `/lt-mem` | Mutator: consolidate, promote, archive, compact, sanitize |
| `/mem-health` | Score memory matrix /100 (6 criteria + v3 trigger checks) |
| `/memory-prune` | Advisory scan for stale or broken entries |
| `/mem-index` | Browse the memory DB: list entries by tier/type, show DB stats |
| `/memory-search` | Search across all agent memory files |
| `/remember` | Meta context save/load (cheaper than compaction) |
| `/good-idea` | Promote a session win to project gotchas |
| `/mistake` | Record a fix for a recurring issue |

Shared scanner: `~/.claude/scripts/scan-mem-matrix.sh`.

### Health (4)

| Skill | Purpose |
|-------|---------|
| `/super-health` | Aggregate /100 (hooks + skills + mem + settings + sessions + comms + regression + subsystems + automations). Always runs the deepest check set (no depth flag); `--complete` adds the 5-agent post-hoc audit |
| `/hook-health` | Score hook subsystem /100 (syntax, perf, naming, coverage) |
| `/skill-health` | Score skill subsystem /100 (frontmatter, refs, descriptions) |
| `/health` | Generic infra health check; `/health [component]` for targeted |

### Workflow (12)

`/brainstorm`, `/commit`, `/fix-issue`, `/notebook`, `/pr`, `/push`, `/rb`, `/review`, `/tdd`, `/verify`, `/wrap-up`

### Meta (8)

`/code-quality`*, `/debugging`*, `/design-review`, `/infra-security`*, `/orchestrator-patterns`*, `/sanity-check`, `/sync-upstream`, `/test-infra`*, `/test-scaffold`

### Domain (6)

`/experiment`, `/gas-patterns`*, `/hpc`, `/research`, `/threat-model`, `/wsl-gotchas`*

### Testing (1)

`/test-cleanup-protocol`

\* = agent-only (not in `/` menu)

**Health quickstart**: `/super-health` for the /100 aggregate score (always the deepest check set).

---

## Memory Matrix

DB-backed persistent memory: a hybrid-search SQLite store at `~/.claude/agent-memory/.memory.db` (FTS5 + vec0). No `MEMORY.md`, `ltm.md`, `mtm.md`, or line budgets. Structure detail: `~/.claude/docs/memory-matrix.md`. Access protocol: `~/.claude/rules/12-agent-hierarchy.md` § Memory Access.

| Tier | Scope |
|------|-------|
| `instance/<agent>` | A single agent's own memory |
| `shared-projects` | One project, all agents |
| `shared-global` | Cross-project, all agents |
| `class` | One agent class |

Types: `feedback`, `project`, `reference`, `user`. Query via `memory_db.py search|get|similar|list` or the `~/.claude/bin/mem` shorthand. Write only through `/remember`, `/good-idea`, `/lt-mem`, `/mistake`; never hand-edit the DB.

---

## Rules (auto-loaded)

| File | Purpose |
|------|---------|
| `00-universal.md` | Read before edit; minimal changes; git discipline; stop conditions |
| `05-coding-standards.md` | Language-specific standards (path-scoped) |
| `10-orchestrator-protocol.md` | Plan/state protocol (in-project + superclaude) |
| `12-agent-hierarchy.md` | Meta/Orch/Worker hierarchy; write scopes; comms; Memory Access SOT |
| `13-worker-first-mandate.md` | Swarm-first defaults; decision boundary; SOT model x effort x thinking matrix |
| `15-programming-principles.md` | DRY, KISS, separation of concerns, defensive design |
| `20-tool-conventions.md` | Universal tool patterns (git -C, parallel batches, merge conflicts, worktrees) |
| `21-domain-gotchas.md` | Stack-specific gotchas (Compose, WSL, Python ns, HDL, large images) |
| `25-context-management.md` | Session lifecycle, context hygiene, self-compact protocol |
| `30-upstream-sync.md` | When to consult `~/.claude/upstream/awesome-claude-code/` |
| `40-swarm-quality-gates.md` | R-1 schema spec, R-2 baseline-stash, R-3 worker verification, R-4 fleet expansion |

---

## Hooks

Modular dispatcher architecture. The main `session-timer.sh` hook dispatches to numbered modules in `hooks/modules/`. Shared helpers live in `hooks/lib.sh`.

| Module | Purpose |
|--------|---------|
| `00-parse.sh` | Parse JSON input (session_id, tool_name) |
| `05-context-check.sh` | File-size-based context estimation |
| `10-nudge.sh` | Non-blocking advisory nudges |
| `15-baseline-stash.sh` | R-2: auto-stash baseline on `/commit false` repos |
| `20-counter.sh` | TDD edit counter (fires at 5 edits without tests) |
| `25-commit-gate.sh` | Conventional commit format check |
| `30-notebook-guard.sh` | Notebook safety guard for `.ipynb` writes |
| `30-timer.sh` | Session time enforcement |
| `40-gc.sh` | PID-liveness garbage collection |
| `45-spawn-log.sh` | Log subagent spawns for `/swarm-status` |
| `50-bootstrap.sh` | Bootstrap freshness check |

Standalone hooks:

| Hook | Event | Purpose |
|------|-------|---------|
| `pre-compact.sh` | PreCompact | Snapshot state files before context compaction |
| `session-cleanup.sh` | SessionEnd | Clean timer files on normal exit |
| `stop.sh` | Stop | Stop-event handling |
| `subagent-stop.sh` | SubagentStop | Subagent completion handling |
| `comms-schema-lint.sh` | PreToolUse (writes to comms) | Schema validation for comms messages |
| `hcom-pre-tool-use.sh` | PreToolUse | HCOM broker integration (Phase D-full; broker canonical for DIR/RPT/ESC) |
| `hcom-session-end.sh` | SessionEnd | HCOM broker session-end cleanup (Phase D-full) |

Details: `~/.claude/rules/25-context-management.md`

---

## HCOM (Phase D-full)

As of 2026-05-09 (Phase D-full), the SQLite-backed message broker (`~/.claude/comms/.broker.db`) is CANONICAL for DIR/RPT/ESC/NUDGE/EVENT: agents read broker content via SQL, not the flat files. The flat-file comms bus (`~/.claude/comms/<orch-name>/`) remains only as Phase B dual-write snapshots for human inspection. Hooks `hcom-pre-tool-use.sh` and `hcom-session-end.sh` are wired in; the broker handles durable concurrent writes, mid-turn message injection, and queryable cross-orch escalations.

Full design: `~/.claude/docs/hcom-design.md`.

---

## Config

**Hierarchy**: User (`~/.claude/`) -> Project (`.claude/`) -> Local (`.claude/*.local.*`). Hooks MERGE across levels.

**Settings**: `~/.claude/settings.json` — permissions, sandbox, hooks. Only scaf edits this file.

**Sandbox**: enabled, filesystem write restricted to `~/projects/cash/`, `~/.claude/`, `/tmp`.

---

## Shell Completions

### Agent Name Completion (bash)

```bash
# Add to ~/.bashrc:
source ~/.claude/scripts/claude-completion.bash
```

### Slash Command Completion (rlwrap)

```bash
# Wrap claude with rlwrap:
rlwrap -f ~/.claude/scripts/claude-completions.txt claude

# Regenerate completions after adding skills:
bash ~/.claude/scripts/generate-completions.sh
```

---

## CLI Reference

| Command | What It Does |
|---------|-------------|
| `/clear` | Clear context between unrelated tasks |
| `/memory` | View/manage auto-memory |
| `/hooks` | Interactive hook configuration |
| `/simplify` | Review changed files (3 parallel agents) |
| `/batch <instruction>` | Parallel changes (5-30 worktree agents) |
| `/loop [interval] <prompt>` | Recurring prompt execution |
| `/model` | Switch model or set effort level |

---

## Sources

- [Sub-agents](https://code.claude.com/docs/en/sub-agents) | [Skills](https://code.claude.com/docs/en/skills) | [Memory](https://code.claude.com/docs/en/memory) | [Hooks](https://code.claude.com/docs/en/hooks)
- Upstream references: `~/.claude/upstream/curated-sources.md`
