# Worker-First Mandate

Applies to ALL spawn-capable agents (meta). NOT applied to `w-*` themselves (they cannot spawn children — wasted tokens).

## The Mandate

**Delegation pattern**: Meta+w-swarm. It is the only delegation pattern.

Infra edits (rules, hooks, skills, CLAUDE.md, wrappers) are handled by meta-supervised w-* swarms (see rules/12).

### Pre-Action Trigger

Before performing ANY task that takes >3 tool calls, ask:

> *"Is this menial enough to delegate to a `w-`? Can I focus my context on synthesis / design / critical thinking / decision-making instead?"*

If YES → delegate. Use `/autocommission` if no existing `w-*` fits the task.

If NO (only retain ownership when):
- Surgical edit ≤50 lines AND no new content/research required
- Single-shot decision needing your full context (architecture, plan synthesis, design call)
- Non-delegatable side effect (writing this canonical SOT itself)

## Model × Effort × Thinking Matrix (SOT)

This is the canonical reference. All other files cross-reference here.

### Critical Implementation Note

Thinking configuration **IS inherited** from the session (v2.1.198+); there is no per-subagent thinking toggle. The depth control is the **effort chain** (highest precedence first): `CLAUDE_CODE_EFFORT_LEVEL` env var > the Agent tool's per-call `effort` parameter (v2.1.292+, persists on resume, not applied to forks) > agent frontmatter `effort:` (one of `low`/`medium`/`high`/`xhigh`/`max`) > inherited session effort.

Prompt-embedded `think` / `think hard` / `think harder` / `megathink` are **dead tokens on adaptive-thinking models** (Opus 4.6/4.7, Sonnet 4.6, Fable); they change nothing. Only `ultrathink` survives, and only as soft in-context guidance, not an effort change.

To get depth in a reviewer or worker, use the frontmatter default or shift it at dispatch per § Effort Assignment. Do NOT rely on a prompt keyword.

This supersedes the prior "embed the keyword in the spawn prompt" guidance, which assumed thinking was not inherited; that instruction is retired.

### Per-Worker Defaults

Models are FIXED per worker (owner decision 2026-10-16): they are chosen for control and to avoid wasteful assignment, not per task. Meta never upgrades a model because a task is long; the only model switches are the ones in the "Model switch" column. The frontmatter of each `~/.claude/agents/w-*.md` mirrors this table.

| Worker | Model | Default effort | Meta's range | Model switch (meta decides) | `maxTurns` |
|---|---|---|---|---|---|
| `w-implementer` | sonnet | high | medium..xhigh | opus when the spec leaves design latitude (matrix below) | 65 |
| `w-doc` | sonnet | medium | low..high | opus (high, range medium..xhigh) for cross-section coherence or Italian client deliverables | 60 |
| `w-reviewer` | sonnet | high | medium..xhigh | opus (high) for architecture reviews | 50 |
| `w-hostile-reviewer` | opus | high | high / xhigh / max by risk (below) | none | 90 |
| `w-planner` | opus | high | medium..xhigh | none | 65 |
| `w-debugger` | sonnet | high | medium..xhigh | opus (high) after 2 failed attempts or for a multi-file race | 65 |
| `w-refactorer` | sonnet | high | medium..xhigh | none | 65 |
| `w-merger` | opus | high | medium..xhigh | none | 65 |
| `w-design-reviewer` | sonnet | medium | low..high | none | 65 |
| `w-tester` | haiku | medium | low..high | none (root causes go to w-debugger) | 30 |
| `w-explorer` | haiku | low | low..medium | none | 40 |
| `w-committer` | haiku | low | low..medium | sonnet for history-rewrite mode | 25 |

**Never `model: fable`** (owner decision 2026-09-23): Opus 5.5 outperforms it on our use cases. Do not dispatch Fable for any worker, helper, or reviewer until the owner lifts this.

### Effort Assignment (per dispatch)

Meta tunes depth with EFFORT, not with model choice. Per dispatch, meta may shift the worker's default effort by one level (the "Meta's range" column) through the Agent tool's `effort` parameter: one level down for an easy, reversible, fully specified task; one level up for a hard one or one where a silent error is costly. This rule is the explicit instruction that authorizes meta to set that parameter. State the chosen effort and the one-line reason in the dispatch log.

**w-implementer matrix.** Model = how much judgment the spec leaves to the worker. Effort = how hard the reasoning is and how costly a silent error would be.

| | medium | high (default) | xhigh |
|---|---|---|---|
| **sonnet** (spec pinned down) | mechanical, ≤3 files, reversible: rename a key, add a flag, re-run an existing chart script | normal feature from a clear spec, tests present | clear spec but tricky logic (heavy parsing, numerics, edge cases), or client-facing output where a quiet error is expensive |
| **opus** (worker must make design calls) | prototype or exploratory script, low stakes | >5 files, a new subsystem, or an ambiguous spec needing judgment | the above plus a wide blast radius: pipelines feeding deliverables, hooks or guards, migrations |

**w-hostile-reviewer risk rule** (the only worker allowed a +2 shift):
- **high**: routine gate; a miss is caught by the next review or by tests.
- **xhigh**: a miss is expensive to fix later (plan gate before a multi-worker wave, a methodology later work builds on).
- **max**: a miss is irreversible or reaches a third party (final acceptance of a client deliverable, a security guard, a published method).

### Turn Caps

`maxTurns` values are set so that a dispatch hits its cap with probability ≤5% at 95% confidence (upper confidence bound on the 95th percentile of turns per job, resumed segments summed, 30 days of transcripts to 2026-10-16; small-sample workers use their family pool). When a cap is hit, Claude Code returns the output marked partial and the run can be resumed with SendMessage; that resume costs wall time, which is what the caps minimize. Do not split a task only to stay under a call count; split it when its parts are independent or its context would not fit. Checkpoint-first (dispatch contract § 6) stays mandatory at any cap. Re-derive the caps when a worker's cap-hit rate in `_spawns.log` evidence exceeds 5%.

Evidence: 458 worker runs, 2026-09-16..2026-10-16 (meta audit, plan `plans/worker-fleet-tuning/`). Per-turn wall time: sonnet ~2x faster than opus on write tasks (0.17 vs 0.36 min/turn implementer); the old "sonnet truncates destructively past ~24 calls" evidence (2026-07-07) predates Sonnet 5.5 and is retired pending new evidence.

### Worker Skills

Two channels only (owner decision 2026-10-16). Workers almost never invoke a skill unprompted (1 Skill call in 458 runs), so "available via the Skill tool" does not count as a channel.
- **Preload** (`skills:` in the agent frontmatter; full content injected at spawn, max 32): skills the worker's job uses on most runs, e.g. `windows-gotchas` for every worker that runs shell commands, plus the worker's own methodology skill.
- **Brief-named**: meta names the situational skills that match the task in the dispatch brief ("load `hpc` with the Skill tool before starting"); the per-worker situational list is in each agent body (§ Situational skills) and in `docs/agent-manifest.md`.
- **Never into a worker**: skills that dispatch a worker themselves (`design-review`, `plan`, `review`, `sanity-check`, `threat-model`) and meta/owner skills (`autocommission`, `swarm-*`, `pleh`, `promote`, `better-super`, `git`, `lt-mem`, `mem-health`, `memory-prune`, `super-health`, `sync-upstream`, `wf-*`).
- A skill used by a worker on most of its dispatches for a month moves to preload; a preloaded skill unused for a month moves to brief-named.

### Effort Level Reference

| Level | Use | Examples |
|---|---|---|
| `low` | Mechanical, lookups, classification | Rename, grep, file recon, conventional commit |
| `medium` | Default agentic work | Standard refactor, doc polish, test addition |
| `high` | Multi-file reasoning, complex debug | Cross-file refactor, race condition, architecture review |
| `xhigh` | Long-horizon agentic loop (Opus 4.7 specific) | Repeated tool calling, deep search, exploratory coding |
| `max` | Frontier problems only | Architecture decisions, security reviews, irreversible migrations |

Note: official Anthropic docs recommend `low` for simple subagent work; our defaults are higher where the job needs reasoning (owner decision 2026-10-16, table above), and § Effort Assignment moves them down for easy tasks.

### Effort × Thinking Orthogonality (Model-Dependent)

| Model | Thinking control |
|---|---|
| Opus 4.7 / Opus 4.6+ / Sonnet 4.6 / Mythos | Adaptive thinking — `/effort` IS the canonical thinking control; manual `budget_tokens` rejected |
| Opus 4.5 / Claude 4 (legacy) | Orthogonal — `/effort` and `budget_tokens` work independently |

In all cases: max-tier thinking on simple/visual tasks ≈10× overspend.

## Battle-Tested Swarm Patterns

### Pattern W-1: Parallel Read-Only Discovery
N up-to-5 read-only Explore/general-purpose helpers, each with bounded scope and word-cap output. **5× wall-time savings** vs sequential.

### Pattern W-4: Parallel Reviewer-in-BG Overlap
After spawning Worker K, immediately dispatch its reviewer with `run_in_background: true`. Proceed to spawn Worker K+1. Reviewer reports back asynchronously. **~40% wall savings**.

### Pattern W-7: Mixed-Type Batch
Up to 5 helpers of different types in parallel: e.g., 3 web research (general-purpose) + 1 code recon (Explore) + 1 reviewer (w-reviewer). **4× speedup**; reviewer catches issues in the parallel synthesis.

### Pattern W-11: Meta-Direct Polish on Swarm-DONE
After a swarm reports DONE, Meta does small polish rounds directly (≤6 files, reversible, no new files, no architecture change). **Saves ~5 hrs** vs another swarm dispatch.

## Critical Rules

**SOT**: `~/.claude/rules/40-swarm-quality-gates.md` (R-1 schema spec, R-2 baseline-stash, R-3 worker verification, R-4 fleet expansion).

## Autocommission Protocol Summary

`/autocommission "<task description>"` (full skill: `~/.claude/skills/autocommission/SKILL.md`):
1. Skill reads task, picks model+effort+thinking+tools from this matrix
2. Writes ephemeral `w-X.md` to `~/.claude/agents/_ephemeral/`
3. Spawns the agent via `Agent({subagent_type: "<ephemeral-name>", ...})` — direct custom-name dispatch (v2.1.63+ Agent tool capability)
4. After task done: cleans up the ephemeral file (DEC-005 Q1: immediate)

Authority: meta only (DEC-005 Q2). Cap: unlimited at this stage (DEC-005 Q3).

## Trigger Escaping (Author-Time)

Three triggers auto-fire on owner's machine the instant their literal token appears in text the CLI or an agent processes: the `.workflow` keyword, the `/.deep-research` command, and the `.ultracode` effort level (shown here dot-escaped; the live tokens omit the leading dot). A stray occurrence in a spawn prompt, report, memory file, plan, or any authored text can fire a costly autonomous run (up to ~1000 agents). **Rule**: whenever you author text another agent or the CLI will process — spawn prompts, memory, plans, skills — keep these three tokens DOT-ESCAPED (leading dot retained) so they never fire. They are strictly owner-opt-in, manual, prompt-level; never bake them into agent defs or skills.

## CCPM Task-Graph Metadata (Phase 5)

In `plan.md`, tasks may declare dependencies and parallelism:

```markdown
### Phase 2 — Worker Fleet Expansion
- T2.1: w-implementer.md  [parallel: true, conflicts_with: T2.6]
- T2.2: w-doc.md           [parallel: true, conflicts_with: T2.6]
- T2.3: w-explorer.md      [parallel: true, conflicts_with: T2.6]
- T2.4: w-tester.md        [parallel: true, conflicts_with: T2.6]
- T2.5: w-committer.md     [parallel: true, conflicts_with: T2.6]
- T2.6: existing-frontmatter-updates  [depends_on: T2.1..T2.5]
```

Convention: `[parallel: true]` declares safe-to-parallelize, `[depends_on: T*]` declares predecessor, `[conflicts_with: T*]` declares mutual exclusion. Meta uses these to auto-decide swarm batches.

## Cross-References

- Hierarchy table: `12-agent-hierarchy.md` (write scopes)
- Context management: `25-context-management.md` (delegation reduces parent context burn)
- Programming principles: `15-programming-principles.md` (DRY/KISS govern w-* design)
- Skills: `/autocommission`, `/swarm-dispatch`
- Plan: `plans/swarm-first-v2/plan.md`
