# Agent Hierarchy Protocol

Chain of command, write scopes, and workspace boundaries for ALL superclaude agents.

## Hierarchy

| Level | Agent | CAN | CANNOT |
|-------|-------|-----|--------|
| Strategic | Meta | Write directives/bootstrap/plans, read reports, manage comms bus, spawn read-only helpers | Edit project code, git in repos, write state.md during Orch execution, edit settings.json |
| Infrastructure | Scaffolder | Edit `~/.claude/` files (agents, hooks, rules, skills, settings.json), validate infra, write own reports | Edit project code, git in repos, architecture decisions alone, remove deny rules or disable sandbox |
| Tactical | Orch / Orch-* | Edit project code, git (except push), spawn workers, write state/reports | Push, architecture decisions alone, write plan.md/directives/bootstrap, touch local `.claude/`, edit settings.json |
| Worker | w-merger, w-debugger, w-refactorer, w-reviewer, w-planner | Edit within assigned scope, run scoped commands | Push, touch local `.claude/`, write to comms, unscoped changes, edit settings.json |

## Multi-Orch

Named orchs (`orch-<name>.md`) are thin aliases referencing `orch.md`. Template in `~/.claude/docs/usage-guide.md`.

**Same-repo parallelism**: git worktrees, non-overlapping file scopes, merge after both complete.
**Cross-project**: fully independent.

## Global Workspace Rule

**All agents run from `~/projects/workspace/` as CWD.** Never `cd` into a project directory.

- Git: `git -C <repo-absolute-path> <command>`
- Files: always absolute paths
- Workers inherit `~/projects/workspace/` as CWD

**NEVER touch** (in ANY project): `<project>/.claude/`, `<project>/CLAUDE.md`

## Write Scope

### Plans and State

| File | Meta | Orch | Workers |
|------|------|------|---------|
| `plans/*/plan.md` | **WRITE** | READ | -- |
| `plans/*/state*.md` | READ (write if no Orch active) | **WRITE** | -- |
| `plans/*/context.md` | **WRITE** | READ | -- |
| `plans/*/mistakes.md` | READ | **WRITE** | -- |

### Communication

Each orch reads/writes ONLY its own `~/.claude/comms/<orch-name>/` directory.

| File | Meta | Orch (own dir) |
|------|------|----------------|
| `directives.md` | **WRITE** | READ |
| `bootstrap.md` | **WRITE** | READ |
| `reports.md` | READ | **WRITE** |
| `escalations.md` | READ + answer | **WRITE** |

**Hard rule**: `plan.md` is NEVER writable by any Orch. Suggest updates via RPT-NNN.

### Enforcement

Before writing ANY `~/.claude/comms/` or `~/.claude/plans/` file: check the tables above. If not in your write scope, **STOP**.

## Communication Protocol

Message formats: `~/.claude/comms/README.md`

**Meta -> Orch**: Write DIR-NNN to directives.md (+ bootstrap.md for new sessions). the user notifies orch.
**Orch -> Meta**: Write RPT-NNN to reports.md. ESC-NNN to escalations.md for blockers.
**Escalation flow**: Orch writes ESC -> the user sees -> the user decides or relays to Meta -> Meta answers below ESC entry.

## Delegation

**Parallel limit**: both Meta and Orch can spawn **up to 5 subagents simultaneously** via the Agent tool. Launch them in a single message with multiple Agent tool calls. Use for parallelizable, independent tasks.

**Meta -> Helpers**: read-only subagents (Explore, general-purpose, w-reviewer, Plan). Helpers must NOT write to code, comms, or state files. See `~/.claude/agents/meta.md` § Helper Subagents.
**Meta -> Orch**: never bypass orch for code-editing workers. Read-only tasks (e.g., w-reviewer) are allowed.
**Orch -> Workers**: absolute paths, full task context, explicit file scope, verify output. See `~/.claude/agents/orch.md` § Delegating to Workers.
