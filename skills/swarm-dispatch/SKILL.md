---
name: swarm-dispatch
description: "Launch parallel w-* batch using W-1/W-4/W-7/W-11 patterns. Meta+orch only."
category: delegation
user-invocable: true
argument-hint: "<pattern> <task spec>"
allowed-tools: Read, Bash, Grep, Glob, Agent
---

# Swarm Dispatch

Codifies the battle-tested swarm patterns (W-1, W-4, W-7, W-11) into a single dispatch skill. Used by meta+orch to launch parallel worker batches with the right shape for the work.

**Authority**: meta + orch only. Workers (`w-*`) cannot spawn children — invoking this skill from a worker is a no-op error.
**Hard cap**: 5 workers per single Agent-tool batch (Anthropic limit).

## Patterns

| Pattern arg | Source | Use for | Worker shape | Wall savings |
|---|---|---|---|---|
| `discovery` | W-1 | Read-only spec capture / recon | N up-to-5 read-only Explore/general-purpose helpers | 5x |
| `reviewer-bg-overlap` | W-4 | Implement K + review K-1 in BG | 1 implementer + 1 reviewer (BG) per cycle | 40% |
| `mixed-batch` | W-7 | Heterogeneous parallel work | up to 5 mixed: web research + code recon + reviewer | 4x |
| `polish` | W-11 | Meta-direct polish on orch-DONE | Meta + 3 read-only helpers | ~5 hr saved per round |

## Procedure

### Pattern: `discovery` (W-1)

1. Parse `N` from task spec (default 5, max 5).
2. Parse the task list — N independent recon questions, each with a bounded scope (file glob, doc section, repo subtree).
3. Dispatch N parallel Agent calls in a single message:
   - `subagent_type: "Explore"` (or `"general-purpose"` for web/external research)
   - Each prompt embeds: scope, single question, **word-cap (≤300 words)**, output schema (R-1).
4. Synthesize the N returns into one verdict in your own words. Cite which worker found what.

### Pattern: `reviewer-bg-overlap` (W-4)

1. Parse the pipeline of K writers (e.g., 3 sequential w-doc tasks).
2. Spawn writer 1, await result.
3. Immediately dispatch w-reviewer with `run_in_background: true` on writer 1's output.
4. Spawn writer 2 (do not wait for reviewer 1).
5. Repeat: each writer's reviewer runs in background while the next writer starts.
6. Collect reviewer reports asynchronously near end of session; act on REJECTs via re-delegate.

### Pattern: `mixed-batch` (W-7)

1. Parse heterogeneous task list and dispatch each helper with the appropriate `subagent_type` matching the work:
   - Code recon → `Explore` or `w-explorer`
   - Code review → `w-reviewer`
   - Implementation → `w-implementer` (with model override per matrix)
   - Doc polish → `w-doc`
   - Web research / generic synthesis → `general-purpose`
   - Mechanical lookup → `w-explorer` (haiku) or `general-purpose` (cheaper for one-off)
2. Verify file scopes do not overlap (R-1 schema spec required if ≥2 workers share output).
3. Dispatch all (≤5) in a single message with multiple Agent calls.
4. Synthesize returns into one combined verdict.

### Pattern: `polish` (W-11)

1. Verify orch state = DONE for the target scope.
2. Verify polish scope: ≤6 files, reversible edits, **no new files**, no architecture change. If violated → STOP, re-handoff to orch.
3. Meta-direct polish: small surgical edits with up to 3 read-only helpers (Explore for context lookups, w-reviewer for sanity check post-polish).
4. Single commit; report polish delta in RPT.

## Direct Dispatch (v2.1.63+)

The Agent tool accepts ANY `subagent_type` matching an agent file in `~/.claude/agents/*.md` (including `_ephemeral/<name>.md` from `/autocommission`).

Prefer specific worker types over `general-purpose` when:
- The task fits a permanent `w-*` agent (per `~/.claude/rules/13-worker-first-mandate.md` matrix)
- An ephemeral agent was just autocommissioned for this exact task
- Model/tool/effort defaults in the agent's frontmatter would be lost if you fall back to general-purpose

Use `general-purpose` only when:
- The task is one-off + research-flavored
- No worker fits AND autocommission would be overkill (≤30s task)
- Mixed batch has both specific and generic work; specific helpers use specific types, generic helpers use general-purpose

Example (W-7 mixed-batch pattern):
```typescript
// 5 parallel helpers in one message:
Agent({subagent_type: "w-implementer", description: "Write Module X", prompt: "..."})
Agent({subagent_type: "w-doc", description: "Polish Section 4", prompt: "..."})
Agent({subagent_type: "Explore", description: "Find usages of API", prompt: "..."})
Agent({subagent_type: "general-purpose", description: "Web research X", prompt: "..."})
Agent({subagent_type: "w-reviewer", description: "Review staged diff", prompt: "..."})
```

## Critical Rules

- Thinking inheritance, the model×effort×thinking matrix, and the R-1/R-3/R-4 gates are the SOT in `rules/13-worker-first-mandate.md` + `rules/40-swarm-quality-gates.md` — follow them; not restated here.
- **5-worker cap**: A single Agent-tool batch may not exceed 5 calls (Anthropic limit). Need more? Sequence batches.
- When authoring spawn prompts, keep `.workflow` / `/.deep-research` / `.ultracode` dot-escaped (see `rules/13-worker-first-mandate.md` § Trigger Escaping (Author-Time)).

## Output Format

### Pre-dispatch (announced before Agent calls)

```
Pattern: <discovery|reviewer-bg-overlap|mixed-batch|polish>
Workers: <N> spawned (types: <Explore x3, w-reviewer x1, ...>)
Schema spec: <path or "n/a (no shared output)">
Scopes: <one-line per worker>
```

### Post-dispatch (after all workers return)

```
| Worker | Status | Files touched | Key findings |
|---|---|---|---|
| Explore #1 | OK | docs/spec.md (read) | Found 3 unspecified edge cases |
| w-reviewer | REJECT | n/a | Test added but assertion weakened |
| ... | | | |

Synthesized verdict: <2-3 line summary, action items>
```

## Examples

### `discovery` — pre-flight recon for LaTeX assignment polish

Spawn 5 parallel Explore helpers reading `report.tex` sections 1-5 (one per helper), each capped at 300 words, returning {section: N, gaps: [...], inconsistencies: [...]}. Synthesize into one polish punch-list.

### `reviewer-bg-overlap` — sequential refactor with concurrent review (example-webapp)

Pipeline: 3 sequential w-implementer tasks editing `src/components/{Hero,Nav,Footer}.tsx`. After each implementer returns, dispatch w-reviewer in BG on the modified file, then immediately spawn the next implementer. Collect 3 reviewer reports near end.

### `mixed-batch` — parallel research + code recon for new feature spec

5 parallel: 2 general-purpose web research (Tailwind v4 migration, Next.js 16 app router changes) + 2 Explore (existing routing code, existing styling code) + 1 w-reviewer on a draft RFC. Synthesize into one design doc.

### `polish` — Meta-direct after orch DONE on example-course example-abm report

Orch reported DONE on `report.tex`. Scope check: 4 files, all reversible (typo fixes, citation order, figure caption tweaks), no new files. Meta dispatches 1 Explore for cite-key cross-check + 1 w-reviewer for final readthrough, then commits.

## Constraints

- **NEVER** exceed 5 workers per single Agent-tool batch.
- **NEVER** skip R-1 schema spec when ≥2 workers in the batch share output.
- **NEVER** invoke this skill from a `w-*` worker — only meta+orch have spawn authority.
- **NEVER** bundle uncertain calls (file lookups that may fail, ref lookups, commands that might error) with safe calls in the same parallel batch — Anthropic cancels ALL siblings if any single call errors. Run discovery calls first, then dispatch from confirmed inputs.
- **NEVER** assume thinking depth propagates — embed keywords per spawn prompt.

## Cross-References

- Patterns SOT: `~/.claude/rules/13-worker-first-mandate.md` § Battle-Tested Swarm Patterns
- Tool conventions: `~/.claude/rules/20-tool-conventions.md` § Parallel Tool Batches
- Worker fleet matrix: `~/.claude/rules/13-worker-first-mandate.md` § Per-Worker Defaults
- Plan: `~/.claude/plans/swarm-first-v2/`
- Sister skills: `/autocommission` (ephemeral worker spawn), `/topology-producer-reviewer` (specialized W-4 wrapper)
