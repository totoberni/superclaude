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

## Loop integration (converge)

`plan` is a ONE-SHOT PRODUCER: a single invocation delegates to `w-planner`, which drafts `plan.md`, regenerates `plan.html`, and writes the memory-DB index card (see Three Outputs above). It has no round-by-round REWORK cycle and emits no SEAL; a full `/converge` loop is not imposed on plan authoring itself.

Before the drafted `plan.md` is relied on, run ONE optional single adversarial review pass, a fresh `w-reviewer` or a `w-planner` self-review checking phase completeness, human-gate identification, and task-graph soundness. This is a single pass, not a loop: one fresh look before use, no VERDICT/REWORK cycling.

For a plan that must itself iterate to a sealed quality bar, the conductor can separately run `/converge` with the drafted `plan.md` as the artefact (methodology/infra class per `/review-dispatch`); `plan` stays one-shot regardless.

Loop orchestration (dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill), per `converge/SKILL.md`'s Conductor context convention.
