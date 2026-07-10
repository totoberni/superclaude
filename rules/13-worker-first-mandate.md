# Worker-First Mandate

Applies to ALL spawn-capable agents (meta, orch, scaf). NOT applied to `w-*` themselves (they cannot spawn children — wasted tokens).

## The Mandate

**Default delegation pattern**: Meta+w-swarm. Ork handoff is the EXCEPTION, reserved for work that genuinely benefits from persistent multi-hour state.

Infra edits (rules, hooks, skills, CLAUDE.md, wrappers) are handled by meta-supervised w-* swarms by default; scaf is legacy/optional (see rules/12).

### Pre-Action Trigger

Before performing ANY task that takes >3 tool calls, ask:

> *"Is this menial enough to delegate to a `w-`? Can I focus my context on synthesis / design / critical thinking / decision-making instead?"*

If YES → delegate. Use `/autocommission` if no existing `w-*` fits the task.

If NO (only retain ownership when):
- Surgical edit ≤50 lines AND no new content/research required
- Single-shot decision needing your full context (architecture, plan synthesis, design call)
- Non-delegatable side effect (writing this canonical SOT itself)

## Decision Boundary: Meta+Swarm vs Ork Handoff

Use **Meta + w-swarm** when ALL of:

```
✓ Estimated wall-clock ≤4 hr (Meta overestimation bias accounted for)
✓ Distinct subtasks <8 with clean independent scopes
✓ No persistent compile-gate ↔ edit-loop coupling required
✓ Total context for synthesis <1M tokens
✓ Single-project scope (no cross-repo coordination needed)
```

Use **Ork (handoff)** when ANY of:
- Multi-day campaign (HPC training, ACM-style multi-hour assembly)
- Persistent compile-gate ↔ edit-loop coupling (LaTeX rebuild every change with iterative state)
- Multi-orch parallelism in same repo (already needs ork-tier coordination)
- Multi-session continuity required (ork preserves identity across hard-blocks)
- HW EDA pipelines (full DC synth → gate-sim → example-tool loop)

**Override**: explicit one-line reason in plan.md or chat justifying the deviation.

A single-artifact convergence loop that meets the Meta+swarm criteria above can also run unattended via the autonomous converge driver: `/wf-auto` configures it, the owner launches it, and the no-pre-approval guards (R-5) are enforced mechanically rather than by a supervising agent. Authority: meta + orch only, same spawn-adjacent boundary as `/converge`.

## Model × Effort × Thinking Matrix (SOT)

This is the canonical reference. All other files cross-reference here.

### Critical Implementation Note

Thinking configuration **IS inherited** from the session (v2.1.198+); there is no per-subagent thinking toggle. The depth control is the **effort chain** (highest precedence first): `CLAUDE_CODE_EFFORT_LEVEL` env var > agent frontmatter `effort:` (one of `low`/`medium`/`high`/`xhigh`/`max`) > inherited session effort.

Prompt-embedded `think` / `think hard` / `think harder` / `megathink` are **dead tokens on adaptive-thinking models** (Opus 4.6/4.7, Sonnet 4.6, Fable); they change nothing. Only `ultrathink` survives, and only as soft in-context guidance, not an effort change.

To get depth in a reviewer or worker, set `effort:` in its agent frontmatter (example: `w-hostile-reviewer` ships `effort: max`) or override model/effort at dispatch. Do NOT rely on a prompt keyword.

This supersedes the prior "embed the keyword in the spawn prompt" guidance, which assumed thinking was not inherited; that instruction is retired.

### Per-Worker Defaults

| Worker | Model | Default `/effort` | Default Thinking | Escalate when |
|---|---|---|---|---|
| `w-explorer` | haiku | low | none | Never |
| `w-doc` (single-section polish) | sonnet | medium | none | Cross-section ⇒ opus + `think` |
| `w-doc` (cross-section coherence) | opus | high | `think harder` | Irreversible structural ⇒ `ultrathink` |
| `w-implementer` (≤3 files) | sonnet | medium | none | Novel ML method ⇒ opus + `think` |
| `w-implementer` (>5 files / web app) | opus | high | `think hard` | Cross-cutting ⇒ `ultrathink` |
| `w-implementer` (HPC SLURM/script) | sonnet | medium | none | New topology ⇒ opus + `think` |
| `w-refactorer` | sonnet | medium | none | Semantic merge ⇒ opus |
| `w-debugger` (single file) | sonnet | medium | `think` | 3 failed attempts ⇒ opus + `think harder` |
| `w-debugger` (multi-file race) | opus | high | `think harder` | n/a |
| `w-merger` (trivial conflicts) | sonnet | medium | none | Semantic conflict ⇒ opus + `think hard` |
| `w-reviewer` (light style/lint) | sonnet | medium | none | Architecture review ⇒ opus + `think hard` |
| `w-reviewer` (`--scathingly-deep`) | opus | high | `think harder` | Irreversible ⇒ `ultrathink` |
| `w-design-reviewer` | sonnet | medium | none | Cross-page consistency ⇒ opus |
| `w-tester` | sonnet | medium | none | Failure root-cause ⇒ delegate to w-debugger |
| `w-planner` (single phase) | opus | high | `think hard` | 3+ phase plan ⇒ `think harder` |
| `w-planner` (architectural) | opus | max | `ultrathink` | n/a |
| `w-committer` | haiku | low | none | History rewrite ⇒ sonnet |

Aggregate distribution if fully adopted: ~5% haiku / ~70% sonnet / ~25% opus.

**"Default Thinking" column superseded**: on adaptive-thinking models (Opus 4.6/4.7, Sonnet 4.6, Fable) the keyword in that column is a no-op; the live depth control is `effort:` in agent frontmatter (see § Critical Implementation Note). The column is retained only as historical reference and for the legacy orthogonal-thinking models in § Effort × Thinking Orthogonality.

### Worker model split (tool-call budget)

Model choice keys on expected tool-call count, not just worker class:
- **<=20 calls**: keep the sonnet-class defaults in the table above.
- **Beyond ~20-24 calls**: override `model: opus` regardless of worker class. Sonnet truncates **destructively** past ~24 calls (half-applied edits land on disk); opus truncation is **report-only** (the work completes underneath, only the closing message is cut).
- **Hard architectural cap ~40 calls / ~250k tokens for all models**: split the task rather than exceed it.

Evidence: concurrent-meta experiment 2026-07-07 (memory `worker-model-sonnet-truncation-opus-experiment`).

### Effort Level Reference

| Level | Use | Examples |
|---|---|---|
| `low` | Mechanical, lookups, classification | Rename, grep, file recon, conventional commit |
| `medium` | Default agentic work | Standard refactor, doc polish, test addition |
| `high` | Multi-file reasoning, complex debug | Cross-file refactor, race condition, architecture review |
| `xhigh` | Long-horizon agentic loop (Opus 4.7 specific) | Repeated tool calling, deep search, exploratory coding |
| `max` | Frontier problems only | Architecture decisions, security reviews, irreversible migrations |

Note: official Anthropic docs explicitly recommend `low` for subagents. Default `w-*` to `medium` only when reasoning is genuinely required.

### Effort × Thinking Orthogonality (Model-Dependent)

| Model | Thinking control |
|---|---|
| Opus 4.7 / Opus 4.6+ / Sonnet 4.6 / Mythos | Adaptive thinking — `/effort` IS the canonical thinking control; manual `budget_tokens` rejected |
| Opus 4.5 / Claude 4 (legacy) | Orthogonal — `/effort` and `budget_tokens` work independently |

In all cases: max-tier thinking on simple/visual tasks ≈10× overspend (per `feedback_effort_keyword_matrix.md`).

## Battle-Tested Swarm Patterns

### Pattern W-1: Parallel Read-Only Discovery
N up-to-5 read-only Explore/general-purpose helpers, each with bounded scope and word-cap output. **5× wall-time savings** vs sequential.

### Pattern W-4: Parallel Reviewer-in-BG Overlap
After spawning Worker K, immediately dispatch its reviewer with `run_in_background: true`. Proceed to spawn Worker K+1. Reviewer reports back asynchronously. **~40% wall savings**.

### Pattern W-7: Mixed-Type Batch
Up to 5 helpers of different types in parallel: e.g., 3 web research (general-purpose) + 1 code recon (Explore) + 1 reviewer (w-reviewer). **4× speedup**; reviewer catches issues in the parallel synthesis.

### Pattern W-11: Meta-Direct Polish on Orch-DONE
After ork reports DONE, Meta does small polish rounds directly (≤6 files, reversible, no new files, no architecture change). **Saves ~5 hrs** vs another orch dispatch.

## Critical Rules

**SOT**: `~/.claude/rules/40-swarm-quality-gates.md` (R-1 schema spec, R-2 baseline-stash, R-3 worker verification, R-4 fleet expansion, R-5 no pre-approval).

## Autocommission Protocol Summary

`/autocommission "<task description>"` (full skill: `~/.claude/skills/autocommission/SKILL.md`):
1. Skill reads task, picks model+effort+thinking+tools from this matrix
2. Writes ephemeral `w-X.md` to `~/.claude/agents/_ephemeral/`
3. Spawns the agent via `Agent({subagent_type: "<ephemeral-name>", ...})` — direct custom-name dispatch (v2.1.63+ Agent tool capability)
4. After task done: cleans up the ephemeral file (DEC-005 Q1: immediate)

Authority: meta + orch only (DEC-005 Q2). Cap: unlimited at this stage (DEC-005 Q3).

## Trigger Escaping (Author-Time)

Three triggers auto-fire on owner's machine the instant their literal token appears in text the CLI or an agent processes: the `.workflow` keyword, the `/.deep-research` command, and the `.ultracode` effort level (shown here dot-escaped; the live tokens omit the leading dot). A stray occurrence in a spawn prompt, directive, report, memory file, plan, or any authored text can fire a costly autonomous run (up to ~1000 agents). **Rule**: whenever you author text another agent or the CLI will process — spawn prompts, comms, memory, plans, skills — keep these three tokens DOT-ESCAPED (leading dot retained) so they never fire. They are strictly owner-opt-in, manual, prompt-level; never bake them into agent defs, skills, or comms.

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
- Skills: `/autocommission`, `/swarm-dispatch`, `/topology-producer-reviewer`, `/super-health`, `/wf-auto`, `/swarm-observe`
- Plan: `plans/swarm-first-v2/plan.md`, `plans/wf-autonomy/plan.md`
