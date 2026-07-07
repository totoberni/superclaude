---
name: plan
description: "Create or update a project plan (superclaude or in-project)."
category: orchestration
user-invocable: true
argument-hint: "<project-goal>"
context: fork
agent: w-planner
---

Create a project plan for: $ARGUMENTS

The w-planner agent handles location decision, codebase analysis, and plan structure. See `~/.claude/agents/w-planner.md` for full protocol.

## Three Outputs Per Plan

Each plan produces three artefacts:

1. **`plan.md`** — the agent-facing **SOURCE OF TRUTH**. Agents read and edit this directly; the planning workflow is unchanged.
2. **`plan.html`** — the **rendered human VIEW**, auto-generated from `plan.md` via `~/.claude/scripts/plan/render_plan.py`. **This is the artefact a human opens in a browser.** It is derived — regenerate it (re-run `render_plan.py <plan.md>`) after any edit to `plan.md`; never hand-edit it.
3. **A memory-DB index CARD** (`plan-index-<slug>`) written by `~/.claude/scripts/plan/plan_index.py`, making the plan discoverable via memory search.
