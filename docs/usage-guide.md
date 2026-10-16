# Superclaude Usage Guide

> Quick reference for `~/.claude/` infrastructure on native Windows 11. Detailed rules auto-load every session.

---

## Architecture

```
~/.claude/
  CLAUDE.md                        # Global user instructions
  settings.json                    # Permissions, hooks, env (hooks and rules written by bin/apply-settings.ps1)
  hooks/                           # dispatch.py + sc_hooks/ (core.py, handlers/)
  rules/                           # Auto-loaded rules (numbered order)
  agents/                          # meta + w-* workers (+ _ephemeral/, _pending_promotion/)
  bin/                             # mem and scpy shims (Git Bash and PowerShell)
  comms/                           # Spawn and outcome logs
  plans/                           # Cross-project plans and checkpoints
  agent-memory/                    # Hybrid-search memory DB (.memory.db)
  skills/                          # Slash commands and agent-loaded skills
  docs/                            # This guide + manifests
  upstream/                        # Curated external references
  scripts/                         # Python utilities (memory, plan, swarm, monitors)
  logs/, tmp/, state/              # Hook error log, scratch files, per-session markers
```

Rules load automatically. Agents activate via Agent tool or `--agent`. Skills appear in `/` menu or get injected into agents at startup. All agents operate from `~/code/`; never `cd` into a project directory; use `git -C <absolute-repo-path>`.

---

## Swarm-First Defaults (v2)

The infrastructure is **swarm-first**: Meta+w-swarm is the only delegation pattern. Full SOT: `~/.claude/rules/13-worker-first-mandate.md`. Quality gates: `~/.claude/rules/40-swarm-quality-gates.md`.

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
| `meta` | `claude --agent meta` | opus | Cross-project coordination, plans, swarm dispatch and review |
| (plain) | `claude` | session default | Ordinary Claude Code session with the rules loaded |

### Launching meta

Launch meta as `claude --agent meta`. In practice use the `meta` launcher function (defined in the owner's shell profile), which runs that command with `CLAUDE_AGENT_NAME=meta` set. The hooks resolve identity from that variable (subagents are identified from the hook payload instead), so a meta session started without it is treated as a plain session by the guards.

### Worker Fleet (12 permanent w-*)

Canonical roster (model, purpose, invocation): `~/.claude/docs/agent-manifest.md` § Worker Fleet.

### Ephemeral Workers (via `/autocommission`)

For one-off tasks where no permanent `w-*` fits, `/autocommission "<task>"` writes a temporary `w-X.md` to `~/.claude/agents/_ephemeral/`, spawns it, and auto-cleans the file when the task finishes. Authority: meta (or the owner's plain session); never a worker.

If an autocommissioned pattern recurs >=3 times across sessions (tracked in the `shared-global` memory tier), `/promote` drafts a permanent `w-*.md` candidate to `~/.claude/agents/_pending_promotion/` for Meta review.

### Hierarchy + Write Scopes

Full table: `~/.claude/rules/12-agent-hierarchy.md`. Worker-first mandate: `~/.claude/rules/13-worker-first-mandate.md`. Quality gates: `~/.claude/rules/40-swarm-quality-gates.md`.

---

## Slash Commands

Canonical skill roster + category breakdown: `~/.claude/docs/agent-manifest.md` § Skills (50 total); per-skill purposes: `~/.claude/skills/README.md`. Memory health quickstart: `/mem-health`.

---

## Memory Matrix

DB-backed persistent memory: a hybrid-search SQLite store at `~/.claude/agent-memory/.memory.db` (FTS5 + vec0). No `MEMORY.md`, `ltm.md`, `mtm.md`, or line budgets. Structure detail: `~/.claude/docs/memory-matrix.md`. Access protocol: `~/.claude/rules/12-agent-hierarchy.md` § Memory Access.

| Tier | Scope |
|------|-------|
| `instance/<agent>` | A single agent's own memory |
| `shared-projects` | One project, all agents |
| `shared-global` | Cross-project, all agents |
| `class` | One agent class |

Types: `feedback`, `project`, `reference`, `user`. Query via `mem search|get|similar|list`. Write only through `/remember`, `/good-idea`, `/lt-mem`, `/mistake`; never hand-edit the DB.

---

## Rules (auto-loaded)

| File | Purpose |
|------|---------|
| `00-universal.md` | Read before edit; minimal changes; git discipline; stop conditions |
| `05-coding-standards.md` | Language-specific standards (path-scoped) |
| `06-writing-style.md` | Writing style (no em or en dashes, plain prose) |
| `07-language.md` | Language policy (English by default; Italian for formal deliverables and code comments) |
| `10-orchestrator-protocol.md` | Plan/state protocol (in-project + superclaude) |
| `12-agent-hierarchy.md` | Meta/Worker hierarchy; write scopes; Memory Access SOT |
| `13-worker-first-mandate.md` | Swarm-first defaults; decision boundary; SOT model x effort x thinking matrix |
| `15-programming-principles.md` | DRY, KISS, separation of concerns, defensive design |
| `20-tool-conventions.md` | Universal tool patterns (git -C, parallel batches, merge conflicts, worktrees) |
| `21-domain-gotchas.md` | Stack-specific gotchas (Compose, Python ns, HDL, large images) |
| `25-context-management.md` | Session lifecycle, context hygiene, self-compact protocol |
| `30-upstream-sync.md` | When to consult `~/.claude/upstream/awesome-claude-code/` |
| `40-swarm-quality-gates.md` | R-1 schema spec, R-2 baseline-stash, R-3 worker verification, R-4 fleet expansion |

---

## Hooks

One Python dispatcher per hook event: `~/.claude/hooks/dispatch.py <Event>`. It loads the handlers under `~/.claude/hooks/sc_hooks/handlers/`, each registered with `@on(event, tools, name)`, and aggregates their results into a single JSON response, so there are no ordering races. A handler that raises is ignored (fail-open, except git-policy, which fails closed) and the error is logged to `~/.claude/logs/hooks.log`.

Guards: write-acl, content-scan, git-policy, commit-gate, git-verb, heuristics, worker-verify, notebook-guard, key-guard. Kill switches: `SUPERCLAUDE_GUARDS=off` (all), `SUPERCLAUDE_GUARD_<NAME>=off|warn|block` (one).

Handler roster by event: `~/.claude/docs/agent-manifest.md` § Hooks. Design: `~/.claude/docs/guard-subsystem.md`. Context lifecycle: `~/.claude/rules/25-context-management.md`.

---

## Config

**Hierarchy**: User (`~/.claude/`) -> Project (`.claude/`) -> Local (`.claude/*.local.*`). Hooks MERGE across levels.

**Settings**: `~/.claude/settings.json` — permissions, env, hooks. Written by `bin/apply-settings.ps1 -Profile superclaude`, run by the owner; agents do not edit it.

**Enforcement**: there is no sandbox on native Windows. Write scopes are enforced by the write-ACL guard plus `permissions.deny`.

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
