# Agent Hierarchy Protocol

Chain of command, write scopes, and workspace boundaries for ALL superclaude agents.

## Hierarchy

| Level | Agent | CAN | CANNOT |
|-------|-------|-----|--------|
| Strategic | Meta | Write directives/bootstrap/plans, read reports, manage comms bus, spawn read-only helpers, superclaude infra edits (rules/hooks/skills/CLAUDE.md/wrappers) via meta-supervised w-* swarms | Edit project code, git in repos, write state.md during Orch execution, edit settings.json |
| Infrastructure | Scaffolder (LEGACY 2026-07-01: optional; routine infra now runs on meta+w-* swarms; retained only for large isolated infra campaigns) | Edit `~/.claude/` files (agents, hooks, rules, skills, settings.json), validate infra, write own reports | Edit project code, git in repos, architecture decisions alone, remove deny rules or disable sandbox |
| Tactical | Orch / Orch-* | Edit project code, git (except push), spawn workers, write state/reports | Push, architecture decisions alone, write plan.md/directives/bootstrap, touch local `.claude/`, edit settings.json |
| Worker | w-merger, w-debugger, w-refactorer, w-reviewer, w-planner, w-design-reviewer, w-implementer, w-doc, w-explorer, w-tester, w-committer (+ ephemeral via `/autocommission`) | Edit within assigned scope, run scoped commands | Push, touch local `.claude/`, write to comms, unscoped changes, edit settings.json, spawn children |

## Scaf status (2026-07-01)

Scaf is a legacy v2 pattern; routine superclaude infra edits (rules, hooks, skills, CLAUDE.md, wrappers) now run via meta-supervised w-* swarms with per-diff verification (R-3), not a separate scaf session. Scaf is retained only for large, isolated infra campaigns, the same way orks are reserved for AOS-scale work. `settings.json` stays the one file meta does not edit (permissions boundary unchanged).

## Multi-Orch

Named orchs (`orch-<name>.md`) are thin aliases referencing `orch.md`. Template in `~/.claude/docs/usage-guide.md`.

**Same-repo parallelism**: git worktrees, non-overlapping file scopes, merge after both complete.
**Cross-project**: fully independent.

## Global Workspace Rule

**Agent CWD**: all agents operate from `~/projects/cash/` (canonical statement: CLAUDE.md § Multi-Orch & Workspace). Never `cd` into a project directory.

- Git: `git -C <repo-absolute-path> <command>`
- Files: always absolute paths
- Workers inherit `~/projects/cash/` as CWD

**NEVER touch** (in ANY project): `<project>/.claude/`, `<project>/CLAUDE.md`

## Write Scope

### Plans and State

| File | Meta | Orch | Workers |
|------|------|------|---------|
| `plans/*/plan.md` | **WRITE** | READ | -- |
| `comms/<name>/state.md` | READ | **WRITE** | -- |
| `plans/*/state.md` (master) | **WRITE** (no Orch active) | READ | -- |
| `plans/*/context.md` | **WRITE** | READ | -- |
| `plans/*/mistakes.md` | READ | **WRITE** | -- |

Per-orch state files live in `comms/<orch-name>/state.md` — colocated with the orch's directives and reports. The master `plans/*/state.md` is Meta's consolidated summary, written only after all orchs complete.

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

**Comms search + HTML (v3)**: for historical/semantic comms queries use `scripts/memory/comms_db.py search` (run `sync` first; hybrid FTS5+vec over all comms). Render entries / report bundles to HTML via `comms_viewer.py`. The broker (`.broker.db`) remains the operational message bus — unread DIR/RPT/ESC are detected via its `read_at`, unchanged. See `comms/README.md` § Comms Search Store + HTML Reports.

## Delegation

**SOT**: `~/.claude/rules/13-worker-first-mandate.md`. That file owns: swarm-first mandate, decision boundary (swarm vs ork), per-worker model × effort × thinking matrix, battle-tested patterns, autocommission protocol summary. R-1/R-2/R-3/R-4 critical rules now live in `~/.claude/rules/40-swarm-quality-gates.md`.

This rule retains only the write-scope tables above (which are hierarchy concerns, not delegation patterns). For ALL delegation guidance, refer to 13-rule.

**Parallel limit** (universal): 5 subagents simultaneously per message via Agent tool. Both Meta and Orch.

**Orch → Workers**: see `~/.claude/skills/_shared/dispatch-contract.md` for the dispatch contract (the SOT); orch.md § Delegating to Workers is a working restatement.

## Memory Access (Canonical for All Spawn-Capable Agents)

Persistent memory lives in a hybrid-search SQLite DB at `~/.claude/agent-memory/.memory.db` (FTS5 lexical + sqlite-vec vector embeddings). There is **no MEMORY.md to read**; query the DB. Hybrid retrieval applies an asymmetric bge-small query-instruction prefix (queries only; stored passages stay bare), widens each arm's candidate pool before fusion, then blends the lexical and vector rankings with Reciprocal Rank Fusion (`k=30`, equal FTS/vector weight). Pass several phrasings in one call (multi-query union) to widen recall. A Tier 2 upgrade (field-split embeddings plus a local cross-encoder reranker, needing a one-time re-embed) is an owner-approved follow-up that is not yet live.

1. **Auto-loaded slice**: at session start, a SessionStart hook injects a pointer + a top-N slice of your most relevant memories. Treat it as your starting context. (Subagents do not get this hook, so they rely on step 2.)
2. **Proactive recall** (do this whenever a task touches prior work, a project, or a known pitfall): run `memory_db.py search "<natural-language query>" -k 8` (see §1 for the full invocation), then `get --name <slug>` for the full body. Hybrid search matches prose, jargon, paths, and error codes. To find memories related to one you already hold (near-dups / same-topic-different-words), use `memory_db.py similar --name <slug>` (hybrid cosine + token-Jaccard; also via `/mem-similar`). `get --name` (and the name argument of `similar`, `prune`, `archive`, `retier`) resolves through a ladder: exact match, path-stem slug, case-insensitive, unique prefix, then an FTS fallback. It never guesses on an ambiguous name; it returns a "did you mean" list instead, so a near-miss slug still resolves.
3. **Scope by tier**: `instance/<your-agent>` (your own memories), `shared-projects` (project gotchas/wins), `shared-global` (cross-project lessons), `class` (your agent-class patterns). Filter with `list --tier <t>` or a natural query.
4. **Write** via the memory skills (/remember, /good-idea, /lt-mem, /mistake); they upsert to the DB. Never write `.md` memory files.

**Search discipline (mandatory before you answer)**: recall is only useful when queried PROPERLY. A single shallow query, no `similar` pass, and reading only snippets has produced answers from assumption when the correct answer was already in the DB. BEFORE answering any prompt that touches a project, tool, machine, convention, or past decision:

1. Run SEVERAL searches with varied vocabulary (synonyms, jargon, file paths, error codes); then run `memory_db.py similar --name <slug>` (hybrid cosine + token-Jaccard) on the closest hit to surface same-topic-different-words rows; then `get --name <slug>` the FULL bodies of the top matches. Never answer from search snippets alone.
2. FLAG every discrepancy explicitly at the TOP of your answer, before proceeding (for example: "memory says X; the plan or my assumption said Y"). Silent divergence produces garbage.
3. Pair recall with empirical verification when state is volatile: memory plus a quick live probe (read the file, grep the code, run the diagnostic). A memory naming a file, flag, or alias is a claim about a past moment; confirm it still holds before acting on it. See the `meta-verify-infra-state-empirically` lesson.

A shorthand wrapper exists at `~/.claude/bin/mem`: `mem search "<q>" [-k N]` | `mem get "<name>"` | `mem similar "<name>" [-k N]` | `mem list [--tier T]`. Prefer it over the long `HF_HUB_OFFLINE=1 ... memory_db.py` invocation to cut token cost.

## Reviewer Attribution on Dirty Trees

Dirty-tree false-positive REJECT mitigation for `/commit false` repos: step 1 (baseline stash) is enforced by `~/.claude/hooks/modules/15-baseline-stash.sh` (auto-stash on first Edit/Write/MultiEdit). Full mitigation (baseline path injected into every `w-reviewer` dispatch, optional explicit scope-list note): `40-swarm-quality-gates.md` R-2.
