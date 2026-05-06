# Tool Conventions

Universal tool-usage patterns learned from past mistakes. Applies to ALL agents.

## Single Source of Truth Across Tool Boundaries

- When a value can be computed in two places (bash vs Python, template vs runtime config, client vs server), pick ONE site as canonical. The other must consume the canonical value directly or not exist.
- **Never mirror a computation for "early failure detection", "clarity", or "documentation"** — the mirror will drift. Sometimes silently (when both sites happen to agree), sometimes catastrophically.
- Classic case: example-project `main.py` constructs `ckpt_path = results_base_folder / dataset / model / exp_name` and creates it via `os.makedirs`. Job scripts that add a bash `CKPT_DIR` variable to pre-create the path "for early permission failure detection" inevitably diverge — and when they do, bash creates a ghost directory and Python writes checkpoints to a different location. 3 occurrences across 4 weeks cost nearly a full training run.
- Rule: if you catch yourself writing a bash formula that mirrors an application-side formula, STOP. Delete the bash mirror, let the application-side error surface at runtime with a clear traceback, fix the root cause (social — chmod/permissions — or structural — choose a distinctive leaf name to avoid the collision).
- Source: G-25 (example-project M-9 3x, session 33 retrospective).

## Superclaude ↔ Local Codebase Firewall

Bidirectional isolation rule — meta-tier agent memory must stay invisible from inside any user project.

- **Local codebase files** (`*.sh`, `*.md`, `*.py`, `*.tex`, `*.cpp`, etc. anywhere under `~/projects/*`) must NOT reference superclaude-internal artefacts by name. Forbidden patterns inside project files include:
  - File/path: `~/.claude/`, `.claude/rules`, `agent-memory`, `shared/projects/`, `class/meta`, `MEMORY.md`, `mtm.md`, `ltm.md`, any project memory filename (`example-project.md`, `example-project.md`, etc.)
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
- **Parallel orchs in the same repo MUST use separate worktrees.** Two orchs doing `git checkout -b` in the same working directory causes a checkout race — the second checkout changes HEAD, and the first orch's next commit lands on the wrong branch. This happened twice (M-001, <project>) and required cherry-pick + force-push to fix. Meta must include worktree setup in directives when dispatching parallel orchs to the same repo.
- NEVER run generative scripts (`scan.py`, `render_html.py`, architecture pipelines) in worktrees — they produce large generated files (`model.json`, `diagram.mmd`, `index.html`) and stale `__pycache__` with worktree-specific paths.
- Stale `.pyc` from worktree runs causes 30+ spurious test isolation failures that look like real bugs.
- Before running tests in a worktree, if unsure of cleanliness: `find . -name "__pycache__" -type d -exec rm -rf {} +`

## Compose / Docker Test Hygiene

- After any test run that uses compose volumes, restore host files dirtied by bind mounts. **Be selective** — `git checkout -- docs/` will also revert intentional edits (e.g., model.json). List specific paths instead of broad directories.
- `sg docker -c "docker ..."` is needed in Claude Code bash tool sessions. The bash tool spawns new shells without the docker group — `sg docker -c` re-enters the group.
- After compose test runs, ALWAYS clean `__pycache__` before running host tests — compose may produce bytecode with container-specific paths.

## WSL File Permissions

- WSL/Windows strips the Unix executable bit (`755 → 644`) on NTFS-mounted files, producing phantom `mode change` diffs with zero content changes.
- All <PROJECT> project repos on WSL should have `core.fileMode=false` set (repo-local, not global) to ignore permission-only diffs.
- **Never commit mode-only diffs** — scripts need `+x` for Docker/VPS deployment. If you see `0 insertions, 0 deletions` staged changes, check `git diff --cached --summary` for mode changes and discard them with `git restore --staged . && git checkout -- .`

## Empirical Verification Before Prescribing

- Before writing a root-cause analysis or fix directive, run a diagnostic command that confirms the hypothesis. Never prescribe from structural reasoning alone.
- For code that constructs file paths from config/args: substitute the ACTUAL runtime values through the construction logic and verify the resulting path string. "It uses `os.path.join`" is not verification.
- Source: GM-1 (4 occurrences across <PROJECT>, VPS, <PROJECT>)

## Python Namespace Gotchas

- When a function is imported via `from module_a import func` into `module_b`, patching `module_a.func` does NOT affect `module_b.func` — it has its own reference. You must patch BOTH: `monkeypatch.setattr("module_a.func", ...)` AND `monkeypatch.setattr("module_b.func", ...)`.
- Prefer `monkeypatch` over `@patch` decorators in test fixtures — monkeypatch auto-restores and doesn't leak between tests. `@patch` can leak if the test errors before the decorator's cleanup runs.
- `del sys.modules["X"]; import X` creates a NEW module object. Code that already imported from the old module still holds OLD references. The fix is save/restore in sys.modules, not re-import.

## HDL Register-Lag in NBA Blocks

- In Verilog non-blocking assignment (NBA) `always @(posedge clk)` blocks, `reg_a <= x; reg_b <= reg_a` reads the OLD value of `reg_a` (pre-clock-edge). This is by design but causes bugs when the intent is to use the newly-computed value.
- Fix: add a PREPARE/SETTLE pipeline state between the register write and the dependent read. Never read a register in the same clock cycle it's written via NBA.
- Source: <PROJECT> M-2 (4 occurrences)

## HDL Forward References (Synopsys DC)

- Synopsys DC PRESTO processes Verilog files top-to-bottom. Forward-referenced wires and regs cause elaboration failures that simulators (VCS, Icarus) silently accept.
- Declare ALL wires/regs BEFORE first use in the file. Declare regs/wires BEFORE `generate` blocks.
- Source: <PROJECT> M-12 (3 occ) + M-12a (4 occ)

## Large Image Handling via Read Tool

- Images loaded via the Read tool consume context proportional to their pixel area. A single image wider than ~2000px OR taller than ~2000px can saturate a large fraction of the available context in a single tool call, especially inside a parallel batch that stacks several images.
- **Protocol**: before Read-ing an image from disk, check its dimensions with `python3 -c "from PIL import Image; im = Image.open('<path>'); print(im.size)"`. If either axis exceeds ~1000px, PIL-crop into tiles each ≤ 1000px and Read each tile in a separate call.
- Crop along natural panel boundaries for multi-panel figures (e.g., a 5-panel horizontal figure → 2-3 tiles at panel edges). Overlap adjacent tiles by ~50-100px so nothing is bisected at the seam and the reader can mentally stitch the view.
- Standard pattern:
  ```python
  from PIL import Image
  im = Image.open('<path>')
  W, H = im.size  # confirm dimensions first
  for i, (x0, x1) in enumerate([(0, 1000), (950, 2000), (1950, W)]):
      im.crop((x0, 0, x1, H)).save(f'/tmp/tile_{i}.png')
  ```
  Then Read each `/tmp/tile_N.png` separately, ideally in parallel tool calls.
- This is a context-preservation discipline, not a rendering-fidelity one — the cropped tiles lose no information, they just spread the context cost across multiple Read calls with room for the rest of the conversation in between.
- Source: <PROJECT> Phase-2 hostile-review figure audits (17 figures at 800-1500px each); <PROJECT> session-43 figure-readability review (s1_2d_correlation_c_sweep.png at 2685×543).
