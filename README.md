# superclaude

Multi-agent CLI infrastructure for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Hierarchical agents, persistent memory, lifecycle hooks, and structured orchestration — all from `~/.claude/`.

## Architecture

```
~/.claude/
  agents/        Agent definitions (meta, scaf, orch, workers)
  hooks/         Lifecycle hooks (session timer, compaction, cleanup)
  rules/         Auto-loaded behavioral rules (numbered order)
  skills/        Slash commands (/review, /plan, /tdd, /health, ...)
  scripts/       Utility scripts (session reaper, infra health)
  comms/         Meta <> Orch communication bus
  agent-memory/  Persistent memory (shared + per-agent)
  plans/         Cross-project orchestration plans
  docs/          Reference documentation
```

## Hierarchy

| Level | Agent | Model | Role |
|-------|-------|-------|------|
| Strategic | `meta` | opus | Cross-project supervision, directives, plan authoring |
| Infrastructure | `scaf` | opus | `~/.claude/` specialist: agents, hooks, rules, skills, settings |
| Tactical | `orch` | opus | Project execution, worker delegation, git, code editing |
| Worker | `w-*` | sonnet | Scoped tasks: review, debug, merge, refactor, plan |

Workers are spawned by orchs via the Agent tool — not launched directly.

## Setup

```bash
git clone https://github.com/<YOUR_USERNAME>/superclaude.git ~/.claude/

# Customize for your environment
$EDITOR ~/.claude/CLAUDE.md       # Your profile and project inventory
$EDITOR ~/.claude/settings.json   # Permissions, sandbox, allowed commands
```

## Usage

```bash
claude --agent meta          # Strategic planning, orch supervision
claude --agent scaf          # Infrastructure edits (settings, hooks, rules)
claude --agent orch          # Direct project work
claude --agent o-<name>      # Named orch instance (project-specific thin alias)
```

## Key Concepts

| Concept | Location | Purpose |
|---------|----------|---------|
| **Rules** | `rules/` | Auto-loaded behavioral constraints. Numbered for order, path-scoped via frontmatter |
| **Skills** | `skills/` | 30 slash commands — user-invocable or preloaded into agents |
| **Comms** | `comms/` | Structured message bus: directives (meta->orch), reports (orch->meta), escalations |
| **Hooks** | `hooks/` | Session timer (35/40/48 min), pre-compaction snapshots, cleanup |
| **Memory** | `agent-memory/` | Shared project knowledge, per-agent instance memory, gotchas and wins |

## Configuration

`settings.json` controls permissions (`allow`/`deny` command lists), sandbox (filesystem + network), and hook registration. Only `scaf` may edit it.

Details: [`docs/usage-guide.md`](docs/usage-guide.md) | Comms protocol: [`comms/README.md`](comms/README.md)

## License

MIT
