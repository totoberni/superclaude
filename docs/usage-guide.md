# Superclaude Usage Guide

> Quick reference for `~/.claude/` infrastructure. Detailed rules auto-load every session.

---

## Architecture

```
~/.claude/
  CLAUDE.md                        # Global user instructions
  settings.json                    # Permissions, hooks
  hooks/                           # Hook scripts
  rules/                           # Auto-loaded rules (numbered order)
  agents/                          # Specialist agents
  comms/                           # Meta <> Orch(s) communication bus
  plans/                           # Cross-project orchestration
  agent-memory/                    # Persistent per-agent memory
  skills/                          # Slash commands and agent-loaded skills
  docs/                            # This guide
```

Rules load automatically. Agents activate via Agent tool or `--agent`. Skills appear in `/` menu or get injected into agents at startup. Project-level `.claude/` overrides global.

---

## Agents

| Agent | Invocation | Model | Purpose |
|-------|------------|-------|---------|
| `meta` | `claude --agent meta` | opus | Cross-project coordination, plans, directives |
| `scaffolder` | `claude --agent scaffolder` | opus | Superclaude infrastructure (agents, hooks, rules, settings.json) |
| `orch` / `orch-<name>` | `claude --agent orch-<name>` | opus | Executes plans, spawns workers, edits code |
| `w-reviewer` | Via Agent tool | sonnet | Read-only code review |
| `w-debugger` | Via Agent tool | sonnet | Debug with gotchas preloaded |
| `w-merger` | Via Agent tool | sonnet | Resolve git merge conflicts |
| `w-planner` | Via Agent tool | opus | Create project plans |
| `w-refactorer` | Via Agent tool | sonnet | Safe targeted refactoring |

Hierarchy, write scopes, CAN/CANNOT: `~/.claude/rules/12-agent-hierarchy.md`

### Named Orch Alias Template

Create `~/.claude/agents/orch-<name>.md`:

```markdown
---
name: orch-<name>
description: "Orch instance for <project> <scope>"
tools: Read, Write, Edit, Bash, Glob, Grep, Agent
model: opus
memory: user
maxTurns: 200
---

# orch-<name>

You are **orch-<name>**, a named orchestrator instance.

- **Comms**: `~/.claude/comms/orch-<name>/`
- **Memory**: `~/.claude/agent-memory/orch-<name>/MEMORY.md`
- **Gotchas**: `~/.claude/agent-memory/shared/projects/<project>.md`

**Startup**: Memory → `~/.claude/agents/orch.md` (full protocol) → bootstrap → plan/state → gotchas → execute directive.
```

### Agent Frontmatter Fields

| Field | Description |
|-------|-------------|
| `name` | Unique identifier (lowercase + hyphens) |
| `description` | When to delegate (Claude uses for auto-routing) |
| `tools` / `disallowedTools` | Allowed/denied tools |
| `model` | `opus`, `sonnet`, `haiku`, or `inherit` |
| `memory` | `user` / `project` / `local` |
| `skills` | Skills to preload (injected at startup) |
| `maxTurns` | Max agentic turns |

---

## Skills

### User-Invocable (`/` menu)

| Skill | What It Does |
|-------|-------------|
| `/commit` | Drafts conventional commit from staged changes |
| `/pr` | Creates GitHub PR with structured summary |
| `/review` | Forks into w-reviewer agent |
| `/plan` | Forks into w-planner agent |
| `/status` | Dashboard: phase progress, git state, TODOs |
| `/handoff` | Structured handoff document for session transitions |
| `/sanity-check <orch>` | Code-reviewer sanity check on an orch's changes |

### Agent-Only (not in `/` menu)

`code-quality` (w-reviewer, w-refactorer), `wsl-gotchas` (w-debugger), `gas-patterns` (w-debugger)

### Bundled (ship with Claude Code)

`/simplify`, `/batch`, `/debug`, `/loop`, `/claude-api`

---

## Config, Memory, Hooks

**Config hierarchy**: User (`~/.claude/`) → Project (`.claude/`) → Local (`.claude/*.local.*`). Higher priority wins. Hooks MERGE (all levels fire).

**Memory**: Auto Memory (`~/.claude/projects/*/memory/MEMORY.md`), Agent Memory (`~/.claude/agent-memory/*/`), `@path` imports in CLAUDE.md.

**Active hooks**: PreCompact (snapshots state files), Session Timer (35min warn → 40min block, meta exempt), SessionEnd (cleanup timer files + session history). **Reaper**: `~/.claude/scripts/session-reaper.sh` (manual zombie cleanup). Details: rule 25.

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

---

## Scheduled Tasks

Claude Code supports `/loop` for recurring task execution:

- `/loop "run /health" every 30 minutes` — periodic infrastructure checks
- `/loop "check deployment status" every 5 minutes for 1 hour` — deployment polling
- `/loop "run tests" in 10 minutes` — one-time delayed execution

Useful for: health monitoring, deployment verification, build babysitting.
Note: loops run in the CURRENT session — they don't survive session end.

---

## Effort Levels

Claude Code supports reasoning effort control:

| Level | Use For | Token Cost |
|-------|---------|------------|
| High | Planning, architecture, complex debugging | ~2x |
| Medium | Default — most coding tasks | 1x |
| Low | Simple edits, formatting, routine fixes | ~0.5x |

Set via `/model` command or in agent frontmatter.
Recommendation: workers on simple tasks → low, orchs → medium, planning → high.

---

## Sources

- [Sub-agents](https://code.claude.com/docs/en/sub-agents) | [Skills](https://code.claude.com/docs/en/skills) | [Memory](https://code.claude.com/docs/en/memory) | [Hooks](https://code.claude.com/docs/en/hooks)
