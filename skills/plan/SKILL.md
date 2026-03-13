---
name: plan
description: "Creates or updates a project plan, either in ~/.claude/plans/ (superclaude) or .orchestrator/ (standalone)."
user-invocable: true
argument-hint: "<project-goal>"
context: fork
agent: w-planner
---

Create a project plan for: $ARGUMENTS

The w-planner agent handles location decision, codebase analysis, and plan structure. See `~/.claude/agents/w-planner.md` for full protocol.
