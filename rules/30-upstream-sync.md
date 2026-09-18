# Upstream Sync Protocol

## Reference Library

The upstream Claude Code community reference lives at `~/.claude/upstream/awesome-claude-code/`. It's a curated catalog of hooks, skills, agents, tools, and workflows.

## When to Sync

- **Meta**: Run `/sync-upstream` before an infra campaign. If the upstream has relevant new resources, note them in the plan.
- **Meta**: When planning new infrastructure features, check if the upstream catalog has existing implementations before designing from scratch.
- **All agents**: When a task references "community best practices" or "upstream patterns", consult `~/.claude/upstream/awesome-claude-code/`.

## Adoption Protocol

When a useful upstream pattern is identified:

1. **Evaluate**: Does it solve a real problem we have? (Not just "cool to have")
2. **Adapt**: Our infrastructure has specific conventions (hierarchy, memory filter). Upstream patterns must be adapted, not copy-pasted.
3. **Dispatch**: Features requiring implementation go through the normal delegation flow (Meta plans -> a meta-supervised w-* swarm executes).
4. **Propagate**: Features that apply to all agents go into rules or hooks. Features for specific agents go into their definitions or skills.

## Stay Current

Feature updates and upgrades from the upstream reference must be applied to all available agents where applicable, unless explicitly excluded. Meta is responsible for evaluating and implementing relevant updates.
