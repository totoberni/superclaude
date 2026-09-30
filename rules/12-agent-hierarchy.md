# Agent Hierarchy Protocol

Chain of command, write scopes, and workspace boundaries for ALL superclaude agents.

## Hierarchy

| Level | Agent | CAN | CANNOT |
|-------|-------|-----|--------|
| Strategic | Meta | Write plans, read worker reports, spawn read-only helpers, supervise w-* swarms, superclaude infra edits (rules/hooks/skills/CLAUDE.md/wrappers) via meta-supervised w-* swarms | Edit project code, git in repos, edit settings.json |
| Worker | w-merger, w-debugger, w-refactorer, w-reviewer, w-hostile-reviewer, w-planner, w-design-reviewer, w-implementer, w-doc, w-explorer, w-tester, w-committer (+ ephemeral via `/autocommission`) | Edit within assigned scope, run scoped commands | Push, touch local `.claude/`, unscoped changes, edit settings.json, spawn children |

A plain session with no `--agent` behaves as an ordinary Claude Code session with the rules loaded.

`settings.json` is owner-run-only: no role edits it. Enforcement is the write-ACL guard plus `permissions.deny`.

**Same-repo parallelism**: git worktrees, non-overlapping file scopes, merge after both complete.
**Cross-project**: fully independent.

## Global Workspace Rule

This section is the canonical statement of the workspace rule.

**Agent CWD**: all agents operate from `~/code/`. Never `cd` into a project directory.

- Git: `git -C <repo-absolute-path> <command>`
- Files: always absolute paths
- Workers inherit `~/code/` as CWD

**NEVER touch** (in ANY project): `<project>/.claude/`, `<project>/CLAUDE.md`

## Write Scope

### Plans and State

| File | Meta | Workers |
|------|------|---------|
| `plans/*/plan.md` | **WRITE** | -- |
| `plans/*/state.md` (master) | **WRITE** | -- |
| `plans/*/context.md` | **WRITE** | -- |
| `plans/*/mistakes.md` | **WRITE** | -- |

**Hard rule**: `plan.md` is NEVER writable by any worker. Suggest updates in the final report.

### Enforcement

Before writing ANY `~/.claude/plans/` file: check the table above. If not in your write scope, **STOP**.

## Delegation

**SOT**: `~/.claude/rules/13-worker-first-mandate.md`. That file owns: swarm-first mandate, per-worker model × effort × thinking matrix, battle-tested patterns, autocommission protocol summary. R-1/R-2/R-3/R-4 critical rules now live in `~/.claude/rules/40-swarm-quality-gates.md`.

This rule retains only the write-scope tables above (which are hierarchy concerns, not delegation patterns). For ALL delegation guidance, refer to 13-rule.

**Parallel limit** (universal): 5 subagents simultaneously per message via Agent tool.

**Meta → Workers**: see `~/.claude/skills/_shared/dispatch-contract.md` for the dispatch contract (the SOT).

## Memory Access (Canonical for All Spawn-Capable Agents)

Persistent memory lives in a hybrid-search SQLite DB at `~/.claude/agent-memory/.memory.db` (FTS5 lexical + sqlite-vec vector embeddings). There is **no MEMORY.md to read**; query the DB. Hybrid retrieval applies an asymmetric bge-small query-instruction prefix (queries only; stored passages stay bare), widens each arm's candidate pool before fusion, then blends the lexical and vector rankings with Reciprocal Rank Fusion (`k=30`, equal FTS/vector weight). Pass several phrasings in one call (multi-query union) to widen recall. A Tier 2 upgrade (field-split embeddings plus a local cross-encoder reranker, needing a one-time re-embed) is an owner-approved follow-up that is not yet live.

1. **Auto-loaded slice**: at session start, the SessionStart memory-injection hook injects a pointer + a top-N slice of your most relevant memories. Treat it as your starting context. (Subagents do not get this hook, so they rely on step 2.)
2. **Proactive recall** (do this whenever a task touches prior work, a project, or a known pitfall): run `mem search "<natural-language query>" -k 8`, then `mem get --name <slug>` for the full body. Hybrid search matches prose, jargon, paths, and error codes. To find memories related to one you already hold (near-dups / same-topic-different-words), use `mem similar --name <slug>` (hybrid cosine + token-Jaccard; also via `/mem-similar`). `mem get --name` (and the name argument of `similar`, `prune`, `archive`, `retier`) resolves through a ladder: exact match, path-stem slug, case-insensitive, unique prefix, then an FTS fallback. It never guesses on an ambiguous name; it returns a "did you mean" list instead, so a near-miss slug still resolves.
3. **Scope by tier**: `instance/<your-agent>` (your own memories), `shared-projects` (project gotchas/wins), `shared-global` (cross-project lessons), `class` (your agent-class patterns). Filter with `mem list --tier <t>` or a natural query.
4. **Write** via the memory skills (/remember, /good-idea, /lt-mem, /mistake); they upsert to the DB. Never write `.md` memory files.

**Search discipline (mandatory before you answer)**: recall is only useful when queried PROPERLY. A single shallow query, no `similar` pass, and reading only snippets has produced answers from assumption when the correct answer was already in the DB. BEFORE answering any prompt that touches a project, tool, machine, convention, or past decision:

1. Run SEVERAL searches with varied vocabulary (synonyms, jargon, file paths, error codes); then run `mem similar --name <slug>` (hybrid cosine + token-Jaccard) on the closest hit to surface same-topic-different-words rows; then `mem get --name <slug>` the FULL bodies of the top matches. Never answer from search snippets alone.
2. FLAG every discrepancy explicitly at the TOP of your answer, before proceeding (for example: "memory says X; the plan or my assumption said Y"). Silent divergence produces garbage.
3. Pair recall with empirical verification when state is volatile: memory plus a quick live probe (read the file, grep the code, run the diagnostic). A memory naming a file, flag, or alias is a claim about a past moment; confirm it still holds before acting on it.

`mem` is on PATH and works identically in Git Bash and PowerShell: `mem search "<q>" [-k N]` | `mem get --name "<name>"` | `mem similar --name "<name>" [-k N]` | `mem list [--tier T]`. It exposes the whole memory CLI (`init`, `search`, `upsert`, `get`, `list`, `prune`, `stats`, `compact`, `similar`, `archive`, `retier`, `export`).

## Reviewer Attribution on Dirty Trees

Dirty-tree false-positive REJECT mitigation for no-commit-projects convention repos (repos where agents do not auto-commit; owner commits manually; detected via `CLAUDE_COMMIT_POLICY` or `hooks/no-commit-projects.local`): step 1 (baseline stash) is enforced by the baseline-stash hook (auto-stash on first Edit/Write/MultiEdit). Full mitigation (baseline path injected into every `w-reviewer` dispatch, optional explicit scope-list note): `40-swarm-quality-gates.md` R-2.
