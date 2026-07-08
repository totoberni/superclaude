---
name: notebook
description: "Use when doing atomic .ipynb edit, execute, or validate work."
category: workflow
user-invocable: true
disable-model-invocation: false
argument-hint: "<subcommand> [options]"
allowed-tools: Bash, Read, Edit, Write
---

# /notebook — Notebook Skill

Programmatic mutation, execution, validation, and multi-agent coordination for Jupyter notebooks. Replaces bespoke `append_t<N>.py` / `fix_group_<X>.py` / `init_notebook.py` patterns. Layered over `nbformat`, `jupytext`, `nbclient`, `nbdime`. Atomic-write + advisory lock + persistent kernel.

**Skill root**: `~/.claude/skills/notebook/`. **CLI entry**: `~/.claude/skills/notebook/notebook` (alias suggested: `nb`).

**Status**: v1.4 (post AT1+AT2 + example-project-review-of-skill-v3 closeout + mandatory-final-sync rule). v1.0 BLOCKERs + HIGHs + MEDIUMs addressed; v1.2-patch closes V3-X1..X5 (linked-worktree paths, fig-binding, heartbeat configurability, output-only commits, nbdime list-source); v1.3 also lands UX-1 (`op: insert_block`), UX-2 (mixed `--cells` selectors), UX-3 (`nb merge-preview`), UX-4 (jupytext PATH capture for merge/pre-commit subshells); v1.4 adds mandatory final-sync rule (H-30).

## Decision tree — when to invoke

| Situation | Action |
|-----------|--------|
| Reading notebook contents | Native `Read` tool — handles `.ipynb` natively. **Do not** use this skill. |
| Source-only edit to an EXISTING cell | Edit the paired `.py` (`Edit` tool), then `nb sync <nb>`. |
| Adding / removing / reordering cells | `nb batch <nb> --plan <plan.yml>` — atomic. |
| Locating a cell by content | `nb find <nb> <pattern> [--in source\|tags\|metadata]`. |
| Executing cells (refresh outputs) | `nb execute <nb> [--cells <range>]` — persistent kernel. |
| Validating after edits | `nb validate <nb>` — schema + AST + LaTeX + firewall. |
| Previewing changes before applying | `nb diff <nb> --plan <plan.yml>` — nbdime textual diff. |
| Snapshot before risky edit | `nb batch <nb> --plan <plan.yml> --snapshot` — saves to `.notebook/snapshots/`. |
| Reverting from a snapshot | `nb revert <snapshot-path>`. |
| First-time setup of a notebook | `nb init <nb>` — pairing + .gitattributes + pre-commit + .notebook/ + kernelspec. |
| Migrating an already-init'd notebook to a new skill version | `nb init <nb> --migrate`. |
| Recovering from sync divergence | `nb regenerate <nb> --from-py` — escape hatch (loses IDs). |
| `.ipynb` lock contention | `nb lock-status <nb>` — shows holder PID. |
| Persistent kernel hung / crashed | `nb reset-kernel <nb>`. |
| Pre-simulating cross-branch merge | `nb merge-preview <nb> <ours> <theirs>` — extracts via `git show + jupytext`, runs `git merge-file` in tempdir, prints conflict topology before the real merge (UX-3). |
| Just finished any `nb batch` / `nb init` / structural edit | **`nb sync <nb>` + verify `<nb>.py` exists on disk (MANDATORY pre-commit — see H-30)** |

## Hard rules — DO and DON'T

**DO**
- **MANDATORY at end of implementation**: run `nb sync <nb>` and verify `<nb>.py` exists on disk before any `git add` / `git commit`. The pre-commit hook (installed by `nb init`) hard-rejects `.ipynb` staged without a paired `.py`. This applies whether the notebook will be committed locally OR pushed — assume commit will be attempted. `nb init` and `nb batch` BOTH attempt an internal `jupytext --sync`, but BOTH can silent-skip when `jupytext --set-formats` failed during init (e.g., read-only `~/.local/share/jupyter/` in sandbox/CI: exits 0 with WARN, paired `.py` never created, `nb validate` still passes). Explicit final sync is non-negotiable. See H-30.
- Edit `.py` (paired) for source-only changes, then `nb sync <nb>`.
- Use `nb batch` for any structural change.
- Run `nb validate` after edits, before commit.
- Run `nb sync <nb>` immediately after `git pull` / branch switch (jupytext is mtime-based; stale-py-newer-than-ipynb causes silent reverse-sync per V1-H10/H13).
- Tag figure-output cells with `metadata: { keep_output: true }` to preserve evidence across nbstripout AND across structural edits (V1-H9).
- Address cells by `cell_id` or `cell_tag` (stable). Use `at_position` only when you've just read the live notebook (cell-shift after `nb init` will invalidate stale positions).
- **NO skill-support files inside a user project workdir.** This is the strict superclaude v2 firewall rule, not a soft preference: agent-tier artefacts of any kind (plan YAMLs, the `.notebook/` dir itself, paired `.py` files, snapshots, kernel-name pointers, `jupytext_path` captures, `warm.py`, `version`, anything else the skill writes) MUST live under `~/.claude/` — typically `~/.claude/notebook/<project>/` — never anywhere under `~/projects/*/`. Plans specifically belong in `~/.claude/comms/<spawning-agent>/notebook-plans/<project>/<wave>.plan.yml` or `$TMPDIR/notebook-plans-<project>/<wave>.plan.yml`, and must be deleted after `nb batch` succeeds (the project's `.notebook/snapshots/` is NOT the rollback SoT under this rule — snapshots also move to `~/.claude/notebook/<project>/snapshots/`). See H-31 + H-32. **The skill in its current shape (v1.4) hard-codes `.notebook/` to the project root and therefore VIOLATES this rule on every `nb init`** — until v2 lands, agents must treat `.notebook/` as a transient working-set, immediately archive its contents under `~/.claude/notebook/<project>/`, and remove the project-side dir before any commit.

**DON'T**
- Edit `.ipynb` directly via `Edit` / `Write` / `NotebookEdit`. Banned and intercepted by `~/.claude/hooks/modules/30-notebook-guard.sh` (PreToolUse, hard-block).
- Write raw json on `.ipynb` files. Banned (pre-commit + PreToolUse hook).
- Call the non-atomic notebook write directly. Use `nb_io.atomic_write_ipynb`.
- Use marker-comment idempotence. Use cell tags + skill state.
- Use try/except import for runtime detection. Use `"google.colab" in sys.modules` (in `nb init`'s emitted runtime probe).
- Use cell tags for execution gating (`nbconvert --execute` ignores them — #1300). Gate in-cell with `if RUNTIME == "colab":`.
- Auto-merge `.ipynb` via nbdime — silent cell-deletion (#597). Use `nbdime mergetool` (interactive) or skill's `jupytext-regen` merge driver.
- Place project under `/mnt/c/` — 9P + no inotify + slow. Skill refuses at init.
- Put a literal `# %%` line in code-cell source (any indentation) — silent cell-split. Skill rejects at write time AND on `nb sync .py → .ipynb`.
- Use `nbstripout` as git filter (clean/smudge) — unresolvable-merge footgun. Use as pre-commit hook only.
- Use `merge=union` for `.ipynb` in `.gitattributes` — invalid JSON. Use `merge=jupytext-regen`.
- **Leave ANY skill-support file inside a user project workdir.** This goes beyond plan YAMLs: `<project>/.notebook/` (kernel name, jupytext_path, warm.py, snapshots/, version, forbidden_imports.txt, plan files, `.merge-driver-installed`, `.requirements.sha256`), the paired `<nb>.py`, and `<project>/plan.yml` are ALL banned under `~/projects/*/`. Per the strict superclaude v2 firewall rule, agent-tier artefacts of every category must live exclusively under `~/.claude/` (typically `~/.claude/notebook/<project>/` for skill state, `~/.claude/comms/<agent>/notebook-plans/<project>/` for plans). `nb init` writing to `<project>/.notebook/` is a v1.4 design defect, not a free pass — see H-31, H-32, and v2-roadmap. The CLI examples and walkthroughs further down the doc predate this rule and are wrong on this dimension; treat all workdir-relative paths in them (`--plan plan.yml`, `cat .notebook/kernel_name`, etc.) as referring to the `~/.claude/notebook/<project>/` mirror, not the project dir.

---

## First-time setup walkthrough (per project)

**One-time per project** — adapt for EXAMPLE_PROJECT, but the steps generalise:

```bash
# 0. Install skill deps into project venv (loose pinning per W4).
cd $HOME/projects/example-course/example-project
.venv/bin/pip install -r ~/.claude/skills/notebook/templates/requirements-skill.txt

# 1. Initialise. This step:
#    - Auto-detects .venv/bin/python and registers `<project>-venv` kernelspec
#      (writes the kernel name to .notebook/kernel_name).
#    - Auto-generates .notebook/warm.py with relevant pre-import blocks
#      uncommented based on `pip list` (qiskit, qiskit-aer, torch, scipy, sklearn).
#    - Appends *.ipynb.lock + .notebook/snapshots/ to .gitignore.
#    - Registers the jupytext-regen merge driver in .git/config.
#    - Captures absolute path of jupytext into .notebook/jupytext_path (UX-4).
#    - Installs the `.git/hooks/pre-commit` rejection (ipynb-without-py),
#      routed via `git rev-parse --git-path hooks` so linked worktrees work (V3-X1).
#    - Injects the runtime-probe cell at index 0 (WARNS LOUDLY about cell shift).
#    - Detects legacy scaffolders (scripts/*.py with raw json on .ipynb)
#      and emits a deprecation hint.
~/.claude/skills/notebook/notebook init example-project.ipynb

# Read the warning carefully: cell indices have shifted by +1 if a probe
# was injected. Address cells by `cell_id` or `cell_tag`, not `at_position`.
# Use `nb find <pattern>` to discover cell IDs by content.

# 2. (Optional) Inspect what got installed.
cat .notebook/kernel_name           # e.g. "example-project-venv"
cat .notebook/warm.py | head -20    # auto-detected blocks
cat .notebook/forbidden_imports.txt # qiskit-deprecated providers shipped by default
```

After init, the project is ready for the canonical agent workflow.

---

## CLI reference

### `nb init <notebook> [--migrate] [--force] [--no-probe-injection]`

Bootstraps a notebook for skill use. Idempotent re-run is safe (`--migrate` makes it explicit). `--force` overwrites existing `.notebook/` config. `--no-probe-injection` skips the runtime-probe cell-0 injection (use when migrating an already-set-up notebook OR when the probe is already present elsewhere).

Refuses:
- Notebook under `/mnt/c/` or `/mnt/d/` (WSL hard rule).
- Bare repo (notebook ops require working tree). Explicit refusal in both `nb_init` and `nb_io.canonical_repo_root` (defence-in-depth).

### `nb batch <notebook> --plan <plan.yml> [options]`

Atomic multi-cell mutation. **Plan format with tags REQUIRED for downstream tag-selector** (V1-H5):

```yaml
operations:
  - op: replace
    cell_id: 5a1b2c3d                   # OR cell_tag: "t5-verify" OR at_position: 55
    new_source: |
      ## 2.2 Gate teleportation and magic state injection
      ...
    tags: ["t5", "section-2-2"]         # REQUIRED if you plan `nb execute --cells "tag:t5"` later
    metadata:
      keep_output: true                 # opt-in: PRESERVE outputs across this replace + nbstripout
  - op: insert
    after_id: 5a1b2c3d                  # OR before_id, OR at_position (<= len(cells) for insert)
    cell_type: code                     # required for insert
    source: |
      import qiskit
      ...
    tags: ["t5-verify"]
  - op: delete
    cell_id: 9e8f7g6h
  - op: reorder
    new_order: [5a1b, 9e8f, 3c4d]       # full ordering; must list every existing cell ID
  - op: insert_block                    # UX-1: insert N cells after one anchor in directive order
    after_id: t5-md-07                  # OR before_id / cell_id / cell_tag / at_position
    cells:                              # appear in directive order, all after the anchor
      - { type: markdown, source: "## §3.1 …", tags: [t6] }
      - { type: code,     source: "import …", tags: [t6, t6-foo], id: t6-foo-01 }
      - { type: markdown, source: "Q(i) …", tags: [t6] }
```

`op: insert_block` is equivalent to N reverse-ordered `op: insert` ops with the same anchor — implemented once in the dispatcher so orchs don't have to predict fresh-UUID `after_id` values across chained inserts. `type` is accepted as alias for `cell_type`. Optional per-cell `id` honours user-supplied stable IDs (validated against nbformat 4.5+ regex).

Options:
- `--lock-timeout <sec>` — default 30 s.
- `--dry-run` — print planned diff via nbdime, do NOT apply.
- `--snapshot` — copy current `.ipynb` to `.notebook/snapshots/<stem>-<ts>.ipynb` before mutation (V1-H19).
- `--force` — override the git-clean working-tree check (default: refuses if `notebook` or paired `.py` are dirty in git, V1-H21).
- `--force-no-example-check` — bypass JupyterLab race detector (dangerous).

Pre-write sequence (V1-H8 + V1-M2): JL check → snapshot if requested → lock acquire → JL re-check → SHA capture (INSIDE lock) → apply plan in-memory → schema validate → SHA recheck just-before-write → atomic-replace → release lock → `jupytext --sync` to .py.

On any failure: aborts, releases lock, no mutation.

### `nb execute <notebook> [--cells <selector>] [options]`

Cells selector grammar (V1-H2 + V1-M1 + UX-2):
- `5:12` — index slice (Python semantics).
- `tag:NAME` — all cells tagged `NAME` (V1-H5: emits WARNING on 0-match instead of silent exit).
- `5a1b2c3d,9e8f7g6h` — comma-separated cell IDs (bare; backward-compat).
- `id:<id1>,id:<id2>,tag:<NAME>` — UX-2 mixed selector: top-level comma split, union of pieces. Each piece must use an explicit prefix (`id:` or `tag:`) when mixed; bare-ID-only and single-mode forms still work as before.
- (omitted) — full notebook.

Options:
- `--cell-timeout <sec>` — per-cell timeout, default 600 s.
- `--lock-timeout <sec>` — default 600 s for execute.
- `--warm-timeout <sec>` — kernel-startup timeout, default 60 s (WSL2 ZMQ binding).
- `--no-warm` — skip auto-warm (assumes kernel already imported the heavy deps).
- `--kernel-name <name>` — override the kernelspec (default: read from `.notebook/kernel_name` written by `nb init`; falls back to system `python3` with WARN).
- `--iopub-heartbeat-timeout <sec>` — V3-X3: default 30 s. Seconds of iopub silence before probing kernel liveness via ZMQ `kernel_info` (V2-C1 watchdog). Bump (e.g. `600`) for long Aer / Monte Carlo simulations whose cells emit no intermediate iopub messages — without this, V2-C1 kills the kernel after 30 s of legitimate silent compute.

First call cold-starts kernel + runs `<dir>/.notebook/warm.py`. Prints `[notebook] cold-start: warming kernel + pre-importing heavy deps (~3 min)…`. Subsequent calls within the same `$CLAUDE_SESSION_ID` reuse the persistent kernel (V1-H1: kernel detached via `start_new_session=True` so it survives CLI exit).

### `nb find <notebook> <pattern> [--in source|tags|metadata]`

Search cells by Python regex. Default `--in source`. Output: `cell[<idx>] id=<id>: <match snippet>`. Useful after `nb init` injects the runtime probe (cell-shift) — locate cells by content, then use the IDs in plans. Argument order is `<notebook>` first (consistent with all other subcommands), `<pattern>` second. (V1-H20)

### `nb revert <snapshot-path>`

Restore notebook from a snapshot file at `<project>/.notebook/snapshots/<stem>-<ts>.ipynb` (created by `nb batch --snapshot`). Resolves the target notebook path from the snapshot's parent-of-parent and notebook stem. (V1-H19)

### `nb sync <notebook>`

`jupytext --sync` wrapper with skill atomic-write + ID-preservation post-process (V1-H10: warns on cell-ID loss; V1-H13: rejects `# %%` literals in `.py` before sync; V1-H14: surfaces jupytext stderr). Idempotent.

### `nb validate <notebook>`

Order: schema validate → AST parse on code cells → pylatexenc on markdown LaTeX → firewall scan (em-dash + superclaude-meta-leak) → forbidden-imports scan.

V1-B2: pylatexenc 2.x + 3.x both supported via compat shim. Default `tolerant_parsing=True` plus a macro allowlist (`\operatorname`, `\bra`, `\ket`, `\braket`, `\mathbb`, `\mathrm`, `\Tr`, etc.).

Exit 0 = pass; non-zero with line/cell pointer on failure.

### `nb diff <notebook> --plan <plan.yml>`

Applies plan to a temp copy, runs nbdime textual diff to stdout. No mutation. Same as `nb batch --dry-run`. (V3-X5: list-form `cell.source` is normalised to a string on a deepcopy before nbdime — prevents `AssertionError` on canonicalised notebooks.)

### `nb merge-preview <notebook> <ours_branch> <theirs_branch> [--base-branch <ref>] [--diff3]`

Pre-simulate `git merge` of two branches' `.ipynb` without engaging the real `jupytext-regen` merge driver or locking the working-tree index. (UX-3, formalises the manual `/tmp/*.py` workflow orchs were doing by hand.)

Pipeline:
1. Resolves `ours`, `theirs`, `base` (default `git merge-base ours theirs`) `.ipynb` blobs via `git show <ref>:<rel>` into a `tempfile.mkdtemp` directory.
2. Converts each to py:percent via `jupytext --to py:percent` (resolved through `.notebook/jupytext_path` if present, else PATH; UX-4 inheritance).
3. Runs `git merge-file [--diff3] ours.py base.py theirs.py` in the tempdir.
4. Walks the merged file for `<<<<<<<` / `>>>>>>>` markers, captures line ranges + nearest preceding `# %% [id]` cell-anchor.
5. Prints conflict count + per-conflict landmarks. Exit 0 = clean (auto-resolves), 1 = conflicts (tempdir retained for inspection — orch can `head` the markers, then deletes manually).

### `nb audit <notebook> [--check <name>] [--warn]`

Read-only audits. Available checks: `em-dash`, `firewall`, `forbidden-imports`, `fig-binding`. (V1-B1 + V3-X2: `fig-binding` regex matches both `savefig("name")` and helper `savefig(fig, "name")`; comparison normalises to `Path(...).stem` so `# fig: report/img/foo.png` ↔ `savefig(fig, "foo")` matches. Per-project regex overrides via `<dir>/.notebook/savefig_pattern.txt`.)

`--warn` flag (V1-H15): soft-warn (exit 0) instead of hard-fail (exit 1). Useful during legacy-cell migration.

### `nb regenerate <notebook> --from-py`

Escape hatch when sync produces divergence. Drops `.ipynb`, rebuilds from `.py` via `jupytext --to ipynb`, atomic-replaces. **Loses cell IDs** (fresh random UUIDs). Use only when paired-sync cannot recover.

### `nb reset-kernel <notebook>`

Drops the persistent KernelManager for the notebook's project, deletes connection file, ends ZMQ session. Next `nb execute` cold-starts. **Now requires the notebook arg** (V1-L3: project resolution is per-notebook, since agents run from `~/projects/workspace/` per global rule).

### `nb lock-status <notebook>`

Reports lock holder (PID + start-time) + liveness probe via psutil if any.

### `nb warm <notebook> [--warm-timeout <sec>] [--kernel-name <name>]`

Manual cold-start trigger. Same as auto-warm in `nb execute`'s first call.

---

## Plan format — tags REQUIRED for downstream tag selectors

(D-5) If your downstream `nb execute` command uses a `tag:NAME` selector, every replace/insert op in the plan must declare `tags: [NAME]`. Otherwise `nb execute --cells "tag:NAME"` matches 0 cells and emits a loud warning (V1-H5) but does NO work.

Concrete example for EXAMPLE_PROJECT's T5 wave: 8 ops, each with `tags: ["t5"]`:

```yaml
operations:
  - op: replace
    cell_id: <id-of-current-T5-marker-cell>   # use `nb find example-project.ipynb "T5"` to discover
    new_source: |
      <!-- T5 marker -->
      ## 2.2 Gate teleportation and magic state injection
      ...
    tags: ["t5"]
  - op: replace
    cell_id: <id-of-T5-verify-code-cell>
    new_source: |
      import numpy as np
      import qiskit
      # ... polished content from scripts/append_t5_gate_teleport.py:90-380
    tags: ["t5", "t5-verify"]
  # ... 6 more ops, all tagged ["t5"]
```

After `nb batch`, `nb execute --cells "tag:t5"` will execute exactly the 8 cells.

---

## `keep_output` contract (V1-H9)

The cell metadata flag `keep_output: true` is honoured at TWO layers:

1. `op: replace` in `nb batch`: if the op (or the existing cell metadata) has `keep_output: true`, the replace preserves outputs and execution_count instead of clearing them. Default is to clear (forces re-execution; matches example-project's selective-clear policy). (V1-H9)
2. nbstripout pre-commit: nbstripout 0.9.0+ honours `metadata.keep_output: true` to preserve outputs across commits. The skill's pre-commit-config.yaml passes `--keep-metadata-keys=cell.metadata.keep_output` so the flag itself survives stripping.

Use case: a figure-output cell whose PNG was validated. Tag the cell with `keep_output: true` to preserve the rendered output across edits to other cells AND across commits.

---

## Multi-orch worktree protocol (Acid Test 2 walkthrough)

(D-3) Two orchs A and B work on non-overlapping cell ranges of the same notebook in separate `git worktree` directories. End state: both orchs' work merged cleanly with no JSON corruption.

```bash
# Setup (the user one-time, before dispatching orchs):
cd $HOME/projects/example-course/example-project
git worktree add ../example-project-t6 -b orch-t6
git worktree add ../example-project-t7 -b orch-t7

# Each worktree MUST run `nb init` (the merge-driver is registered in
# .git/config which IS shared across worktrees by default, BUT the
# .notebook/.merge-driver-installed marker is per-worktree as a defence-in-depth).
cd ../example-project-t6 && ~/.claude/skills/notebook/notebook init example-project.ipynb --no-probe-injection
cd ../example-project-t7 && ~/.claude/skills/notebook/notebook init example-project.ipynb --no-probe-injection
# Use --no-probe-injection in worktrees to avoid re-injecting the probe — it's already there.

# Each orch runs in its own worktree:
# orch-t6 in ../example-project-t6/: appends T6 cells (tags: ["t6"])
# orch-t7 in ../example-project-t7/: appends T7 cells (tags: ["t7"])
# Skill's flock on <canonical-root>/<rel>.ipynb.lock serialises writes
# to the same physical inode (same canonical-root regardless of worktree).

# After both orchs complete, merge:
cd $HOME/projects/example-course/example-project           # main worktree
git merge orch-t6                              # text-merges .py + jupytext-regen drives .ipynb
git merge orch-t7
~/.claude/skills/notebook/notebook validate example-project.ipynb
```

The `jupytext-regen` merge driver:
- Drops the conflicting `.ipynb` content.
- Calls `jupytext --to ipynb <merged.py> -o <merged.ipynb>`.
- Exit 0 unconditionally (no manual conflict resolution needed).
- UX-4: resolves `jupytext` via `<repo>/.notebook/jupytext_path` (captured by `nb init` from the user's PATH); falls back to `$PATH` lookup. Sidesteps git merge-driver subshell stripping `.venv/bin`.

V3-X1: linked worktrees are now first-class — `nb init` from a linked worktree resolves `.git/hooks/` via `git rev-parse --git-path hooks` (which routes to the canonical hooks dir even when `<worktree>/.git` is a gitdir-pointer-file). Lock files use `relative_to(worktree_root)` for the rel path but live under the canonical root, preserving the shared-lock invariant across worktrees of the same notebook.

V3-X4: post-execute commits with output-only `.ipynb` diffs (re-execution refresh of figures) no longer need `--no-verify` — the pre-commit hook compares the staged source-only py:percent view against `git show HEAD:<py>` and allows when equal.

UX-3: orchs can `nb merge-preview <nb> <ours> <theirs>` BEFORE invoking the real `git merge` to inspect conflict topology in a tempdir without touching the working tree.

This sidesteps `nbdime` auto-merge's silent-cell-deletion footgun (#597, open since June 2021).

---

## Architecture

**Paired mode (default)**: `.py` (jupytext py:percent) + `.ipynb` both committed. `.ipynb` is authoritative for cell IDs (jupytext `--sync` preserves IDs from `.ipynb` side via content matching; orphan-`.py` reads randomise IDs per W2 empirical). Agents normally edit `.py` for source; structural changes route through `nb batch`. The skill is the SSoT controller — it owns `.ipynb` writes and propagates to `.py` via sync.

**Cell IDs**: nbformat 4.5 random-UUID per JEP-62 (assigned-on-create, stable post-creation). Anchor links use markdown HTML (`<a id="...">`) or cell tags — never IDs.

**Atomicity**: every `.ipynb` write goes through `nb_io.atomic_write_ipynb` — `tempfile.NamedTemporaryFile(dir=path.parent, newline="\n")` → flush → fsync → `os.replace` + dir-fsync. POSIX-atomic on WSL2 ext4.

**Concurrency**: cross-orch serialisation via `fcntl.flock(LOCK_EX|LOCK_NB)` on a sentinel sidecar file. Lock path: `relative_to(worktree_root)` (worktree-local) anchored at the canonical root (shared) so cross-worktree locks of the same notebook converge to the same file (V3-X1). Per-notebook granularity. `flock` auto-releases on SIGKILL. Lock files are auto-`.gitignore`d by `nb init` (V1-H7).

**Persistent kernel**: `jupyter_client.KernelManager` keyed by `(project, $CLAUDE_SESSION_ID)`. Connection file `~/.cache/notebook-skill/kernels/<project>-<session>.json`. Spawned with `start_new_session=True` (V1-H1) so the kernel survives the CLI process exit and persists across `nb execute` invocations within the orch session. Liveness via `client.kernel_info(reply=True, timeout=2)` ZMQ probe. Crash recovery: `cleanup_resources(restart=False)` + explicit `cf.unlink(missing_ok=True)` (jupyter_client #941: cleanup leaks). Connection file is chmod 600 from birth (V1-H11: umask before `start_kernel`).

**Multi-orch worktree merge**: text-merge `.py`, drop `.ipynb`, regenerate via `jupytext --sync` (custom git merge driver `jupytext-regen` installed by `nb init`). nbdime auto-merge is forbidden (silent cell-deletion #597 — open since 2021).

**JupyterLab race detection**: `psutil.process_iter() + p.open_files()` to detect JL holding the file + mtime drift check (100 ms threshold for FS-granularity-blind). Detector runs PRE-lock AND POST-lock-acquire (V1-M2). Hard-fail with offending PID. JL 4.x writes `.ipynb` directly with no `.ipynb_checkpoints/` shadow — psutil is the canonical detector.

**Runtime detection** (emitted as `.ipynb` cell 0 by `nb init`): `"google.colab" in sys.modules` (no try/except — preferred per yandex Practical_RL #294). Plus env-var probes for Kaggle/Codespaces/Binder/JupyterHub. All gating in-cell via `if RUNTIME == "colab":` — never via cell tags (`nbconvert --execute` ignores them).

**LaTeX in markdown**: edit in `.py` py:percent, default `# `-comment markers — LaTeX backslashes never enter Python lexer, byte-exact round-trip empirically verified for `\langle`, `\dagger`, `\mathrm`, multi-line align environments. Eliminates the example-project-vs-example-project convention-drift bug surface.

---

## Banned-pattern enforcement layers

- L1 — `SKILL.md` (this file): self-policing. Required reading by agents.
- L2 — `~/.claude/hooks/modules/30-notebook-guard.sh` (PreToolUse, hard-block via `exit 2`):
  - `Edit`/`Write` with `file_path` ending `.ipynb` → block.
  - `NotebookEdit` tool call → block.
  - `Bash` with `python -c|-m` invocation containing raw json on `.ipynb` → block.
  - `Bash` with `jupyter nbconvert --inplace` on `.ipynb` → block.
- L3 — `<project>/.git/hooks/pre-commit` (installed by `nb init`):
  - Reject `.ipynb` staged without paired `.py`.
  - Grep staged files for forbidden-pattern strings.

---

## Acid tests (corrected per V1.1 fixes)

(D-2) Acid tests derived from the example-project problem catalog (dev review):

### Acid Test 1 — T5 polish-loop fix

```bash
cd $HOME/projects/example-course/example-project

# 0. Install skill deps.
.venv/bin/pip install -r ~/.claude/skills/notebook/templates/requirements-skill.txt

# 1. Initialise. Auto-installs <project>-venv kernelspec, auto-detects qiskit
#    in warm.py, injects runtime probe at cell 0 (WARNS — cells shift +1).
~/.claude/skills/notebook/notebook init example-project.ipynb

# 2. Discover the IDs of the (post-shift) T5 cells. Use the marker text from
#    the existing scaffolder content as a search anchor:
~/.claude/skills/notebook/notebook find example-project.ipynb "Gate teleportation"
# → yields the cell IDs of the T5 cells.

# 3. Author plan.yml addressing each T5 cell BY cell_id (not at_position),
#    each op tagged ["t5"]. The polished content lives at:
#      $HOME/projects/example-course/example-project/scripts/append_t5_gate_teleport.py:90-380
#    Convert each `CELL_T5_*` constant into one `op: replace` with `tags: ["t5"]`.

# 4. Preview.
~/.claude/skills/notebook/notebook diff example-project.ipynb --plan plan.yml

# 5. Apply atomically with snapshot for rollback safety.
~/.claude/skills/notebook/notebook batch example-project.ipynb --plan plan.yml --snapshot

# 6. Execute T5 cells with project venv kernel + warm pre-import.
~/.claude/skills/notebook/notebook execute example-project.ipynb \
    --cells "tag:t5" \
    --warm-timeout 240 \
    --cell-timeout 600

# 7. Validate.
~/.claude/skills/notebook/notebook validate example-project.ipynb

# 8. Verify acid-test pass criterion.
ls -lh report/img/s2_2_gate_teleport_fidelity.png

# 9. (Optional) audit fig-binding. Existing EXAMPLE_PROJECT cells use a `savefig(...)` helper
#    NOT `plt.savefig(...)`. The default regex catches both. If the project has
#    a custom helper, configure via .notebook/savefig_pattern.txt. Use --warn for
#    grandfathered cells.
~/.claude/skills/notebook/notebook audit example-project.ipynb --check fig-binding --warn
```

If something goes wrong: `nb revert <path-to-snapshot>` rolls back to pre-batch state.

### Acid Test 2 — Two parallel orchs T6 + T7

See "Multi-orch worktree protocol" above.

---

## Troubleshooting

- "Lock held by another process": another orch is writing. `--lock-timeout <sec>` to increase wait. Default 30 s for edit, 600 s for execute. Use `nb lock-status <nb>` to see PID. (Lock files are gitignored by default.)
- "JupyterLab has notebook open (PID X)": close JL or `kill <PID>`.
- "Kernel cold-start timeout": bump `--warm-timeout` (default 60 s for WSL2 ZMQ binding).
- "py-content-similarity below threshold; ID assignment unstable" OR "jupytext --sync lost N cell ID(s)": emitted on suspect sync (V1-H10). Inspect with `nb diff`; `nb regenerate --from-py` if confirmed divergence.
- "Banned pattern detected at <file>:<line>": pre-commit / PreToolUse hook flagged. Replace per the banned-pattern table.
- "Kernel died (exit code N)": `nb reset-kernel <nb>` then re-run. State lost; re-execute upstream cells.
- (D-6) "pylatexenc not available" / wrong version: install `pylatexenc>=2.10` in the project venv. Skill supports both 2.x and 3.x via compat shim (V1-B2). If `nb validate` still false-fails on `\operatorname`/`\bra`/`\ket`/`\braket`, the macro allowlist may need extending — file a bug citing the failing fragment.
- "runtime probe injected at cell index 0 (cells shifted +1)": emitted by `nb init` (V1-H3). All existing `at_position` references in plans are now off by one. Use `nb find <pattern>` to re-derive cell IDs.
- "DEPRECATION HINT: found N legacy scaffolder(s)": `nb init` (V1-H18) detected `scripts/*.py` files using raw json/nbformat patterns against `.ipynb`. The PreToolUse hook will hard-block them on next agent run. Migrate to `nb batch` after the first AT pass.
- "working tree has uncommitted changes to <path>": V1-H21 git-clean check. Either commit/stash, or pass `--force` to `nb batch`.
- "snapshot failed": check disk space + `.notebook/snapshots/` permissions.

---

## Known limitations (v1.1)

- Single-host only (no multi-host coordination).
- Free Colab from agent = edit-only (skill writes Colab-ready cells, runs `nbconvert --execute` locally to validate); execution on Colab requires opt-in `claude-colab` bridge with manual bootstrap by the user.
- Multi-orch worktree warm-cost: 2-4 min × N orchs cold-start (no shared-kernel pool — state-leak risk dominates). Acceptable since 3+ parallel-orch flows are rare on notebooks.
- jupytext py:percent on code cells with literal `# %%` in source: hard reject at write time AND on `nb sync .py → .ipynb` (V1-H13). No silent-escape.
- Schema validate enforces JSON schema only; semantic graph validation (broken cell-id refs, missing kernels) is the orch's responsibility.
- Random-UUID cell IDs are NOT content-addressable — agents needing "find the verify cell" should use cell tags (`tag:t5-verify`), `nb find <pattern>`, or content-hash the cell themselves.
- IDs may be reassigned by jupytext on a content-mismatch fallback during `--sync`. The skill emits a WARN (V1-H10) but cannot recover lost IDs without manual intervention.
- `--migrate` is a thin idempotent re-run of all install steps (v1.1 schema). True schema migrations (when v2 ships) will need chained migrators.

---

## Cluster H — known patterns and footguns (D-8)

These were discovered during stress-test research (W1-W5) + example-project-meta validation.

- H-1: Pre-write `ast.parse()` per code cell catches Python syntax errors before they land. Implemented in `nb validate`.
- H-2: SHA256 figure-cache sidecar — idempotent regen only when input hash changes (example-course L8 pattern). Punted to v2.
- H-3: Canonical Colab-runtime fork pattern — `"google.colab" in sys.modules` (NOT `try/except`). Skill emits as cell 0 via `nb init`.
- H-4: Centralised audit pipeline (em-dash + firewall + forbidden-imports + fig-binding) replaces example-project/example-cw forks. `nb audit`.
- H-5: Selective output clearing (example-project policy) — clear code outputs by default, preserve markdown. `keep_output: true` opts-in to preserve code outputs too.
- H-6: LaTeX-string convention drift across projects — solved by py:percent eliminating Python string layer entirely.
- H-7: Catalog correction P-C2 — EXAMPLE_PROJECT scripts DID assign IDs (index-derived). The defect was stability, not absence.
- H-8: `# %%` literal in code source = silent cell-split. Skill rejects at write AND on sync (V1-H13).
- H-9: jupytext `--sync` is mtime-based, not content-hash. `git checkout` updates mtimes; post-checkout sync direction can flip. Protocol: always `nb sync` after pull/branch-switch.
- H-10: jupyter_client `cleanup_resources` leaks connection file (#941). Skill always follows with explicit `cf.unlink(missing_ok=True)`.
- H-11: JL 4.x writes `.ipynb` directly without `.ipynb_checkpoints/` shadow — psutil-based detector is canonical.
- H-12: kernelspec auto-discovery — `nb init` auto-registers `<project>-venv` kernelspec (V1-B3). System `python3` falls back with WARN.
- H-13: warm.py auto-detect — `nb init` runs `pip list` and uncomments relevant blocks (V1-H4).
- H-14: cell-shift safety — `nb init`'s probe injection emits LOUD warning; `nb find` available for ID re-discovery (V1-H3).
- H-15: `fig-binding` regex configurable per-project via `<dir>/.notebook/savefig_pattern.txt` (V1-B1).
- H-16: pylatexenc 2.x + 3.x compat + macro allowlist (`\operatorname`, `\bra`, `\ket`, etc.) (V1-B2).
- H-17: `nb execute --cells "tag:X"` reports "0 cells matched" loudly (V1-H5).
- H-18: merge-driver registration is per-clone — written to shared `.git/config` (visible across worktrees) PLUS per-worktree `.notebook/.merge-driver-installed` marker (V1-B4).
- H-19: jupytext `--sync` ID-loss detection — pre/post-set diff with WARN (V1-H10).
- H-20: `nb execute` cold-start tax addressed via `start_new_session=True` detach — kernel persists across CLI invocations within the session (V1-H1).
- H-21: linked-worktree path resolution (V3-X1) — `lock_path_for` uses worktree-local `relative_to` anchored at canonical (shared lock); `assert_git_clean_or_force` runs `git status` against the worktree's index; `_install_pre_commit_hook` resolves hooks dir via `git rev-parse --git-path hooks`.
- H-22: `fig-binding` audit canonicalises both `# fig:` comment and savefig-name to `Path(...).stem` so helper-form `savefig(fig, "foo")` matches `# fig: report/img/foo.png` (V3-X2).
- H-23: `nb execute --iopub-heartbeat-timeout <sec>` — heartbeat watchdog is configurable for long-silent simulations (default 30s for V2-C1 fast-fail intent on dead kernels) (V3-X3).
- H-24: pre-commit hook output-only exemption — staged `.ipynb` whose `jupytext --to py:percent` view byte-equals `git show HEAD:<py>` is allowed (V3-X4). PreToolUse hook still covers the original "raw `.ipynb` edit" threat model.
- H-25: `nb diff` source-list normalisation — list-form `cell.source` is `"".join`-ed on a `copy.deepcopy` before nbdime to avoid its `diff_strings_linewise` AssertionError (V3-X5).
- H-26: `op: insert_block` (UX-1) — block insert with N cells after a single anchor; reverse-iterates internally so the public order in YAML matches the post-execution order (no UUID prediction needed).
- H-27: mixed `--cells` selectors (UX-2) — `id:abc,id:def,tag:t5` works via top-level comma split + union; bare-ID-only and single-mode forms preserved (backward compat).
- H-28: `nb merge-preview` (UX-3) — pre-simulate cross-branch `.ipynb` merges in a tempdir; tags conflicts with surrounding `# %% [id]` cell-anchors. Exit 1 retains the tempdir for orch inspection.
- H-29: `.notebook/jupytext_path` (UX-4) — `nb init` captures the resolved absolute jupytext binary so the merge-driver and pre-commit-hook subshells (which don't inherit `.venv/bin` in PATH) can find it.
- H-30: `nb init`'s `jupytext --set-formats` step silently exits 0 with WARN if `~/.local/share/jupyter/` is read-only (sandbox/CI). Subsequent `nb batch`'s internal `jupytext --sync` is a no-op (no format registered). The `.ipynb` passes `nb validate` cleanly but the paired `.py` is absent — the pre-commit hook then hard-rejects the commit with `REJECT: <nb>.ipynb staged without paired <nb>.py`. **Mitigation**: agents MUST run `nb sync <nb>` as a final step after all implementation is complete and verify `<nb>.py` exists on disk before staging. `// TODO consider: make nb init HARD-FAIL (exit non-zero) when jupytext --set-formats returns non-zero, rather than WARN-and-continue — the current silent-success contract is the root cause of this footgun.`
- H-31: **Plan-file location footgun — skill predates the firewall.** `nb batch --plan <p>` accepts any path, and every example in this doc historically shows `--plan plan.yml` (workdir-relative) or `--plan .notebook/<wave>.plan.yml` — both VIOLATE the superclaude firewall (`~/.claude/rules/20-tool-conventions.md` §Superclaude ↔ Local Codebase Firewall, which bans agent-tier artefacts from user project workdirs). `nb init` only gitignores `.notebook/snapshots/`; plan files dropped alongside (`*.plan.yml`) get tracked and ride into the commit. **Mitigation**: agents put plan YAMLs in `~/.claude/comms/<spawning-agent>/notebook-plans/<project>/<wave>.plan.yml` or `$TMPDIR/notebook-plans-<project>/<wave>.plan.yml`, and delete after `nb batch` succeeds. The DO/DON'T section above is the canonical rule; the historical examples below it are wrong on this dimension and should be read with the plan path substituted. `// TODO consider: nb batch --plan SHOULD warn (or refuse) when the resolved plan path is under the project repo root; nb init SHOULD gitignore '.notebook/*.plan.yml' alongside snapshots/.`
- H-32: **The `.notebook/` directory itself violates the firewall — v2 redesign required.** `nb init` writes ALL skill state to `<project>/.notebook/` (kernel name, warm.py, jupytext_path, snapshots/, version, forbidden_imports.txt, merge-driver marker, requirements sha). Per the strict superclaude v2 rule no agent-tier artefacts may populate user project workdirs — not just plans. The current `.gitignore` discipline (gitignoring `.notebook/snapshots/` and untracking `.notebook/*` cached files) only addresses what reaches REMOTE; the local presence is itself the violation. **Interim mitigation** (until v2 lands): after running `nb init` or `nb batch`, immediately `rsync -a <project>/.notebook/ ~/.claude/notebook/<project>/ && rm -rf <project>/.notebook` and similarly archive the paired `<nb>.py`. Subsequent `nb batch` / `nb sync` calls will recreate `.notebook/` — repeat the move each session, OR avoid the skill until v2. **v2 roadmap** (canonical task): refactor `nb_io.canonical_repo_root` + `nb_init` to resolve all state paths under `$XDG_DATA_HOME/notebook-skill/<project-hash>/` (or `~/.claude/notebook/<project>/`); the project dir holds only the user-authored `.ipynb`. Pre-commit hook, merge driver, lock files all need re-anchoring. Tracked as H-32; no version assigned yet.

---

## See also

- WSL gotchas: `~/.claude/skills/wsl-gotchas/`
- Project memory (per project): query `memory_db.py search "<project> gotchas mistakes" -k 6` (shared-projects tier)
