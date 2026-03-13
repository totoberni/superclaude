# Tool Conventions

Universal tool-usage patterns learned from past mistakes. Applies to ALL agents.

## Git with `-C`

- `git -C <dir>` sets the repo working directory. All pathspecs after it are **relative to the repo root**.
  - WRONG: `git -C /path/to/repo checkout --ours /path/to/repo/file.txt`
  - RIGHT: `git -C /path/to/repo checkout --ours file.txt`
- Always use the **full absolute path** for `-C` (e.g., `git -C $HOME/projects/workspace/example-enterprise-app`). Never use relative paths.

## Remote-Only Branches

- Many project branches exist only as remotes (never checked out locally).
- Before referencing any branch name, run `git -C <repo> branch -a` to confirm whether it's local or remote-only.
- Use `origin/` prefix for remote-only branches (e.g., `origin/codex/live` not `codex/live`).

## Parallel Tool Batches

- Claude Code cancels ALL sibling calls in a parallel batch if ANY single call errors. Applies to ALL tool types (Bash, Read, Grep, etc.), not just Bash.
- Never group uncertain calls (files that might not exist, ref lookups, commands that might fail) with safe calls in the same parallel batch.
- Common trap: first-session MEMORY.md doesn't exist yet — read it separately before other files.
- Pattern: run safe discovery calls first, then use results to construct the next batch.

## Merge Conflicts

- `git merge` returns exit code 1 when there are conflicts. This is expected workflow, not a failure.
- Proceed to conflict resolution (manual or via w-merger agent). Don't treat as a retry-able error.
- After resolution, always verify with `git -C <repo> diff --check` (detects leftover conflict markers).

## Worktree Hygiene

- **Meta** creates and deletes worktrees. **Orchs** keep them clean during use.
- **Parallel orchs in the same repo MUST use separate worktrees.** Two orchs doing `git checkout -b` in the same working directory causes a checkout race — the second checkout changes HEAD, and the first orch's next commit lands on the wrong branch. This happened twice (M-001, example-project) and required cherry-pick + force-push to fix. Meta must include worktree setup in directives when dispatching parallel orchs to the same repo.
- NEVER run generative scripts (`scan.py`, `render_html.py`, architecture pipelines) in worktrees — they produce large generated files (`model.json`, `diagram.mmd`, `index.html`) and stale `__pycache__` with worktree-specific paths.
- Stale `.pyc` from worktree runs causes 30+ spurious test isolation failures that look like real bugs.
- Before running tests in a worktree, if unsure of cleanliness: `find . -name "__pycache__" -type d -exec rm -rf {} +`

## Compose / Docker Test Hygiene

- After any test run that uses compose volumes, restore host files dirtied by bind mounts. **Be selective** — `git checkout -- docs/` will also revert intentional edits (e.g., model.json). List specific paths instead of broad directories.
- `sg docker -c "docker ..."` is needed in Claude Code bash tool sessions. The bash tool spawns new shells without the docker group — `sg docker -c` re-enters the group.
- After compose test runs, ALWAYS clean `__pycache__` before running host tests — compose may produce bytecode with container-specific paths.

## WSL File Permissions

- WSL/Windows strips the Unix executable bit (`755 → 644`) on NTFS-mounted files, producing phantom `mode change` diffs with zero content changes.
- All EXAMPLE_PROJECT project repos on WSL should have `core.fileMode=false` set (repo-local, not global) to ignore permission-only diffs.
- **Never commit mode-only diffs** — scripts need `+x` for Docker/VPS deployment. If you see `0 insertions, 0 deletions` staged changes, check `git diff --cached --summary` for mode changes and discard them with `git restore --staged . && git checkout -- .`

## Python Namespace Gotchas

- When a function is imported via `from module_a import func` into `module_b`, patching `module_a.func` does NOT affect `module_b.func` — it has its own reference. You must patch BOTH: `monkeypatch.setattr("module_a.func", ...)` AND `monkeypatch.setattr("module_b.func", ...)`.
- Prefer `monkeypatch` over `@patch` decorators in test fixtures — monkeypatch auto-restores and doesn't leak between tests. `@patch` can leak if the test errors before the decorator's cleanup runs.
- `del sys.modules["X"]; import X` creates a NEW module object. Code that already imported from the old module still holds OLD references. The fix is save/restore in sys.modules, not re-import.
