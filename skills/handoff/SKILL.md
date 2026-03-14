---
name: handoff
description: "Orch lifecycle: commission, check-in, parallelize, or session handoff"
category: orchestration
user-invocable: true
disable-model-invocation: true
argument-hint: "[--commission <project> [name]] [--continue] [--parallelize <plan>] [<target>]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /handoff — Orch Lifecycle Manager

Parse `$ARGUMENTS`: `--commission` | `--continue` | `--parallelize` | `<target>` (default: session handoff)

---

## Mode 1: Commission (`--commission <project> [name]`)

1. **Survey**: Read `plan.md`, `meta-registry.md`, `shared/projects/<project>.md` (pitfalls)
2. **Decide (DEC-NNN)**: Reuse idle orch if scope fits, else scaffold new. Record rationale.
3. **Scaffold** — create **all 6 artifacts** (missing any = broken orch):

| # | Artifact | Location |
|---|----------|----------|
| 1 | Agent alias | `~/.claude/agents/<name>.md` (template: `docs/usage-guide.md`, name: `o-<project>-<seq>`) |
| 2 | Comms dir | `~/.claude/comms/<name>/` with `{directives,bootstrap,reports,escalations}.md` |
| 3 | DIR-001 | `comms/<name>/directives.md` — include `### Known Pitfalls` (3-7 items) |
| 4 | Bootstrap | `comms/<name>/bootstrap.md` — identity, env, top 3 pitfalls inline, plan/state refs |
| 5 | Registry | Append to `~/.claude/comms/meta-registry.md` Active table |
| 6 | State stub | `~/.claude/plans/<project>/state-<name>.md` |

Tell the user: "Start `claude --agent <name>` in a new terminal."

---

## Mode 2: Check-in (`--continue`)

1. **Identify**: Read `meta-registry.md` → list Active orchs
2. **Assess** (parallel reads: reports, escalations, state):

| Signal | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| Last RPT | DONE / IN_PROGRESS | Same task >2 RPTs | BLOCKED |
| Escalations | None pending | ESC <30 min | ESC >30 min |
| Commits | Regular | None >20 min | None in session |

3. **Act**: Healthy → report "on track". Warning → corrective DIR. Critical → flag to the user. DONE → flag for review + decommission instructions.

---

## Mode 3: Parallelize (`--parallelize <plan-path>`)

1. **Extract scopes**: file dirs/files per directive
2. **Conflict graph**: nodes=directives, edges=overlapping scope. Greedy largest-scope-first batching.
3. **Same-repo**: REQUIRED worktree setup (checkout races = M-001, 3x). Non-overlapping file scopes.
4. **Output**: `| Batch | Orch | DIRs | File Scope | Constraint |`

---

## Default: Session Handoff (`<target>` or no args)

1. Summarize: objective, completed, remaining, files modified (`git diff --name-only`), decisions, blockers
2. Write to: `comms/<target>/bootstrap.md` (orch target) | `.orchestrator/handoff-<date>.md` (in-project) | chat (no target)
3. Include memory load order for receiver: instance MEMORY.md → shared/projects → class/mtm.md → global/ltm.md