# Tool Conventions

Universal tool-usage patterns learned from past mistakes. Applies to ALL agents.

## Sweep Verification: Full Enumeration, Never Head-Limited

- Context-hygiene head limits (`head_limit`, `| head -N`) are for EXPLORATION only. Any grep whose result gates a claim (absence/presence checks, "class closed"/"all corrected" sweeps, R-3 verification, doc-truthfulness audits) must enumerate the FULL hit list, or use per-file counts (`grep -c`) instead of a truncated match list.
- Sweep for LINE-WRAPPED token variants in prose: a backtick citation split across a line break defeats single-line patterns. Add an end-of-line variant (`pattern$`) alongside the plain grep.
- Never write a universal claim ("every X corrected", "zero Y remain") in a commit message or report without a fresh zero-hit class grep at write time.
- Source: meta 2026-07-07 (cotformer: `head -8` produced a false WANDB_MODE absence claim) + 2026-07-08 (automations W5.1a: `head -12` truthfulness sweep missed a 4th sibling file, 11 citations and 1 line-wrapped token; cost a full hostile-gate REJECT round). Occ=2, promoted. Memory: `meta-verification-grep-discipline`.

## Single Source of Truth Across Tool Boundaries

- When a value can be computed in two places (bash vs Python, template vs runtime config, client vs server), pick ONE site as canonical. The other must consume the canonical value directly or not exist.
- **Never mirror a computation for "early failure detection", "clarity", or "documentation"** — the mirror will drift. Sometimes silently (when both sites happen to agree), sometimes catastrophically.
- Classic case: example-project `main.py` constructs `ckpt_path = results_base_folder / dataset / model / exp_name` and creates it via `os.makedirs`. Job scripts that add a bash `CKPT_DIR` variable to pre-create the path "for early permission failure detection" inevitably diverge — and when they do, bash creates a ghost directory and Python writes checkpoints to a different location. 3 occurrences across 4 weeks cost nearly a full training run.
- Rule: if you catch yourself writing a bash formula that mirrors an application-side formula, STOP. Delete the bash mirror, let the application-side error surface at runtime with a clear traceback, fix the root cause (social — chmod/permissions — or structural — choose a distinctive leaf name to avoid the collision).
- Source: G-25 (example-project M-9 3x, session 33 retrospective).

## Superclaude ↔ Local Codebase Firewall

Bidirectional isolation rule — meta-tier agent memory must stay invisible from inside any user project.

- **Local codebase files** (`*.sh`, `*.md`, `*.py`, `*.tex`, `*.cpp`, etc. anywhere under `~/code/*`) must NOT reference superclaude-internal artefacts by name. Forbidden patterns inside project files include:
  - File/path: `~/.claude/`, `.claude/rules`, `agent-memory`, `shared/projects/`, `class/meta`, `MEMORY.md`, `mtm.md`, `ltm.md`, any project memory filename (`example-project.md`, `example-project.md`, etc.)
  - Superclaude memory artefacts (MD or DB): `.memory.db`, `memory_db.py`
  - Identifiers: `M-\d+`, `MM-\d+`, `GM-\d+`, `G-\d+`, `MT-\d+`, `CW-\d+`, `W-\d+` when used as agent-memory cell references (NOT when they are local section IDs like reprod-notes.md's own `C1`/`B4` etc. — those stay).
  - Phrases: "meta says", "memory.md says", "according to the gotchas file", "see the project memory".
- **Before any write to a local project file**, grep the draft for the forbidden patterns and strip them. Replace by:
  - (a) a local file reference if the content exists locally (`docs/reprod-notes.md §C4`), or
  - (b) inline paraphrase with no meta-structure reference.
- **Superclaude memory files** (`~/.claude/agent-memory/**/*.md`, `~/.claude/rules/**/*.md`) MAY freely reference local project paths and content. The flow is one-way: meta reads local, local does not read meta.
- **Why**: a teammate cloning the project repo, a reviewer reading a paper submission, or a future-you on a different machine sees the local files only. References to meta files resolve to nothing and leak internal tooling.
- Source: G-27 (example-project S33 retrospective, 4 contaminated files stripped).

## Git with `-C`

- `git -C <dir>` sets the repo working directory. All pathspecs after it are **relative to the repo root**.
  - WRONG: `git -C /path/to/repo checkout --ours /path/to/repo/file.txt`
  - RIGHT: `git -C /path/to/repo checkout --ours file.txt`
- Always use the **full absolute path** for `-C` (e.g., `git -C ~/code/example-enterprise-app`). Never use relative paths.

## Remote-Only Branches

- Many project branches exist only as remotes (never checked out locally).
- Before referencing any branch name, run `git -C <repo> branch -a` to confirm whether it's local or remote-only.
- Use `origin/` prefix for remote-only branches (e.g., `origin/codex/live` not `codex/live`).

## Parallel Tool Batches

- Claude Code cancels ALL sibling calls in a parallel batch if ANY single call errors. Applies to ALL tool types (Bash, Read, Grep, etc.), not just Bash.
- Never group uncertain calls (files that might not exist, ref lookups, commands that might fail) with safe calls in the same parallel batch.
- Common trap: a call that may fail — a `mem search` before the venv/DB is confirmed, a ref lookup, a maybe-missing file — run it separately, not batched with safe calls.
- Pattern: run safe discovery calls first, then use results to construct the next batch.

## Two Shells: Git Bash and PowerShell

- Commands may run in either the Bash tool (Git Bash) or the PowerShell tool. Quoting and escaping rules differ between them (single vs double quotes, `$` expansion, backslash vs backtick escapes, `&&` availability), so a command string that is correct in one shell can be silently altered in the other.
- **Rule**: never pass a body of text (memory bodies, commit messages, markers such as `<!-- ... -->`, regexes, file content) inline on a command line. Write it to a file with the **Write tool** (exact bytes, no shell layer), e.g. `~/.claude/tmp/<name>.md`, then pass the file path (`--text-file ~/.claude/tmp/<name>.md`).
- Prefer the `mem` and `scpy` shims, which behave identically in both shells. Verify a content write by reading the stored value back, not by re-typing the same string in a shell predicate.

## Merge Conflicts

- `git merge` returns exit code 1 when there are conflicts. This is expected workflow, not a failure.
- Proceed to conflict resolution (manual or via w-merger agent). Don't treat as a retry-able error.
- After resolution, always verify with `git -C <repo> diff --check` (detects leftover conflict markers).

## Worktree Hygiene

- **Meta** creates and deletes worktrees. **Workers** keep them clean during use.
- **Parallel workers in the same repo MUST use separate worktrees.** Two agents doing `git checkout -b` in the same working directory causes a checkout race — the second checkout changes HEAD, and the first agent's next commit lands on the wrong branch. This happened twice (M-001, <project>) and required cherry-pick + force-push to fix. Meta must include worktree setup in the dispatch prompt when dispatching parallel workers to the same repo.
- NEVER run generative scripts (`scan.py`, `render_html.py`, architecture pipelines) in worktrees — they produce large generated files (`model.json`, `diagram.mmd`, `index.html`) and stale `__pycache__` with worktree-specific paths.
- Stale `.pyc` from worktree runs causes 30+ spurious test isolation failures that look like real bugs.
- Before running tests in a worktree, if unsure of cleanliness: list the `__pycache__` directories under the worktree (Glob `**/__pycache__`) and delete those named paths. No recursive wildcard deletes.

## Empirical Verification Before Prescribing

- Before writing a root-cause analysis or fix instruction, run a diagnostic command that confirms the hypothesis. Never prescribe from structural reasoning alone.
- For code that constructs file paths from config/args: substitute the ACTUAL runtime values through the construction logic and verify the resulting path string. "It uses `os.path.join`" is not verification.
- Source: GM-1 (4 occurrences across <PROJECT>, VPS, <PROJECT>)

## Domain-Specific Gotchas

Moved to `21-domain-gotchas.md` (auto-loaded next). HDL/Verilog/Synopsys, Python namespace, test working-tree hygiene, large image handling.
