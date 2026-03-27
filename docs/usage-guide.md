# Superclaude Usage Guide

> Quick reference for `~/.claude/` infrastructure. Detailed rules auto-load every session.

---

## Architecture

```
~/.claude/
  CLAUDE.md                        # Global user instructions
  settings.json                    # Permissions, hooks, sandbox
  hooks/                           # Dispatcher + modular hooks
  rules/                           # Auto-loaded rules (numbered order)
  agents/                          # Specialist agents
  comms/                           # Meta <> Orch(s) communication bus
  plans/                           # Cross-project orchestration
  agent-memory/                    # 3-tier memory matrix
  skills/                          # Slash commands and agent-loaded skills
  docs/                            # This guide + manifests
  upstream/                        # Curated external references
  scripts/                         # Utilities (health, completions, reaper)
```

Rules load automatically. Agents activate via Agent tool or `--agent`. Skills appear in `/` menu or get injected into agents at startup.

---

## Agents

| Agent | Invocation | Model | Purpose |
|-------|------------|-------|---------|
| `meta` | `claude --agent meta` | opus | Cross-project coordination, plans, directives |
| `scaf` | `claude --agent scaf` | opus | Superclaude infrastructure (agents, hooks, rules, settings.json) |
| `orch` / `o-<proj>-<seq>` | `claude --agent o-<name>` | opus | Executes plans, spawns workers, edits code |
| `w-reviewer` | Via Agent tool | sonnet | Read-only code review (3 modes: general/infra/security) |
| `w-debugger` | Via Agent tool | sonnet | Debug with gotchas preloaded |
| `w-merger` | Via Agent tool | sonnet | Resolve git merge conflicts |
| `w-planner` | Via Agent tool | opus | Create project plans |
| `w-refactorer` | Via Agent tool | sonnet | Safe targeted refactoring |
| `w-design-reviewer` | Via Agent tool | sonnet | Frontend design review (7-phase) |

Hierarchy, write scopes, CAN/CANNOT: `~/.claude/rules/12-agent-hierarchy.md`

### Orch Naming Convention

New named orchs use `o-<project>-<seq>` (e.g., `o-example-project-1`, `o-example`). Legacy `orch-<name>` names continue to work.

### Named Orch Alias Template

See `~/.claude/docs/agent-manifest.md` for full details. Create `~/.claude/agents/o-<project>-<seq>.md`:

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
- **Memory**: `~/.claude/agent-memory/instance/o-<project>-<seq>/MEMORY.md`
- **Gotchas**: `~/.claude/agent-memory/shared/projects/<project>.md`

**Startup**: Memory -> `~/.claude/agents/orch.md` (full protocol) -> bootstrap -> plan/state -> gotchas -> execute directive.
```

---

## Skills (46 total, grouped by category)

| Category | Skills |
|----------|--------|
| **workflow** (10) | `/brainstorm`, `/commit`, `/fix-issue`, `/pr`, `/push`, `/rb`, `/review`, `/tdd`, `/verify`, `/wrap-up` |
| **orchestration** (9) | `/delegate`, `/handoff`, `/nudge`, `/observe`, `/plan`, `/pleh`, `/portfolio`, `/session-reaper`, `/status` |
| **memory** (8) | `/compact-mem`, `/good-idea`, `/mem-health`, `/memory-prune`, `/memory-search`, `/mistake`, `/remember`, `/sanitize-mem` |
| **meta** (9) | `/code-quality`*, `/debugging`*, `/design-review`, `/infra-security`*, `/orchestrator-patterns`*, `/sanity-check`, `/sync-upstream`, `/test-infra`*, `/test-scaffold` |
| **health** (4) | `/health`, `/hook-health`, `/skill-health`, `/super-health` |
| **domain** (6) | `/experiment`, `/gas-patterns`*, `/hpc`, `/research`, `/threat-model`, `/wsl-gotchas`* |

\* = agent-only (not in `/` menu)

**Health assessment**: Run `/super-health --quick` for /100 aggregate score.

---

## Memory Matrix

3-tier persistent memory. Details: `~/.claude/docs/memory-matrix.md`

| Row | Scope | Path | Budget |
|-----|-------|------|--------|
| Shared | Cross-agent | `shared/global/ltm.md`, `shared/projects/<proj>.md` | 60 lines each |
| Class | Per agent type | `class/<class>/mtm.md` | 40 lines |
| Instance | Per agent | `instance/<name>/MEMORY.md` | 80/40/30 lines |

All paths relative to `~/.claude/agent-memory/`. Root symlinks provide shorthand access (e.g., `agent-memory/meta/` -> `instance/meta/`).

---

## Hooks

Modular dispatcher architecture. All hooks run through `session-timer.sh` which dispatches to `hooks/modules/`:

| Module | Purpose |
|--------|---------|
| `00-parse.sh` | Parse JSON input (session_id, tool_name) |
| `05-context-check.sh` | File-size-based context estimation |
| `10-nudge.sh` | Non-blocking advisory nudges |
| `20-counter.sh` | TDD edit counter (fires at 5 edits without tests) |
| `25-commit-gate.sh` | Conventional commit format check |
| `30-timer.sh` | Session time enforcement (35/40/48 min) |
| `40-gc.sh` | PID-liveness garbage collection |
| `50-bootstrap.sh` | Bootstrap freshness check |

Other hooks: `pre-compact.sh` (PreCompact snapshots), `session-cleanup.sh` (SessionEnd).

Details: `~/.claude/rules/25-context-management.md`

---

## Config

**Hierarchy**: User (`~/.claude/`) -> Project (`.claude/`) -> Local (`.claude/*.local.*`). Hooks MERGE across levels.

**Settings**: `~/.claude/settings.json` — permissions, sandbox, hooks. Only scaf edits this file.

**Sandbox**: enabled, filesystem write restricted to `~/projects/workspace/`, `~/.claude/`, `/tmp`.

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
