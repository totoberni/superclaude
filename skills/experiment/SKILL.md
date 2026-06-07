---
name: experiment
description: "Track ML experiments: list, add, compare metrics, suggest next."
category: domain
user-invocable: true
disable-model-invocation: true
argument-hint: "<list|add|compare|status|next> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Experiment Tracker

Manage ML experiment records via `experiments.md` in any project. File-based only — no ML library imports.

**Usage**: `/experiment <subcommand> [args]`

## Project Detection

Detect the ML project from `$ARGUMENTS` or infer from context:
1. Explicit path: `/experiment add $HOME/projects/workspace/<project> ...`
2. Named project: `/experiment list <project>` → resolve to `~/projects/workspace/<project>/`
3. If ambiguous, ask the user

Set `PROJECT` to the absolute project path. All file operations use absolute paths.

Check the memory DB for project-specific gotchas before any operations:
```bash
HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py \
  search "<project> gotchas mistakes" -k 6
```

## Subcommands

### `list` — List all experiments

Read `$PROJECT/experiments.md`. Display as a formatted table:

```
| ID | Name | Date | Status | Key Metric | Notes |
```

If `experiments.md` doesn't exist, report "No experiments tracked yet. Use `/experiment add` to start."

### `add` — Record a new experiment

**Args**: `add <name> [--config <path>] [--metrics <path>] [--notes "..."]`

1. Read `$PROJECT/experiments.md` (create if missing with template below)
2. Auto-increment experiment ID (EXP-NNN)
3. If `--config` provided, read the config file and extract key hyperparameters
4. If `--metrics` provided, read `summary.json` or metrics file:
   - If file missing, log `metrics: pending` (graceful handling)
   - If file exists, extract final metrics (loss, accuracy, perplexity, etc.)
5. Append new row to the experiments table
6. If `--notes` provided, add to Notes column

**Template** for new `experiments.md`:

```markdown
# Experiments

| ID | Name | Date | Status | Config | Key Metrics | Notes |
|----|------|------|--------|--------|-------------|-------|
```

### `compare` — Compare experiments side-by-side

**Args**: `compare <EXP-ID-1> <EXP-ID-2> [<EXP-ID-3>...]`

1. Read `$PROJECT/experiments.md`
2. Find the referenced experiments
3. For each, attempt to read its config and metrics files (paths from the table or `$PROJECT/results/<exp-name>/summary.json`)
4. Output a comparison table:

```
| Dimension | EXP-001 | EXP-002 |
|-----------|---------|---------|
| Model     | ...     | ...     |
| LR        | ...     | ...     |
| Loss      | ...     | ...     |
| Notes     | ...     | ...     |
```

Handle missing `summary.json` gracefully — show "N/A" for unavailable metrics.

### `status` — Check experiment status

**Args**: `status [EXP-ID]`

1. If EXP-ID given, show detailed status for that experiment
2. If no EXP-ID, show summary of all experiments:
   - Count by status (running, completed, failed, pending)
   - Most recent experiment
   - Best performing experiment (by primary metric if available)
3. Check for SLURM output files (`slurm-*.out`) in the project if HPC context detected

### `next` — Suggest next experiment

**Args**: `next [--strategy <grid|random|ablation>]`

1. Read `$PROJECT/experiments.md` to understand what's been tried
2. Analyze patterns:
   - What hyperparameters have been explored?
   - Which configs produced best results?
   - What hasn't been tried yet?
3. Suggest 2-3 concrete next experiments with rationale:
   - Recommended config changes
   - Expected impact based on trends
   - Priority ranking

Default strategy: ablation (vary one thing at a time). Grid and random generate broader sweeps.

## Constraints

- File management only — never import wandb, tensorboard, or ML libraries
- All paths absolute
- Handle missing `summary.json` gracefully (don't error, show "pending" or "N/A")
- Respect project-specific gotchas (e.g., <PROJECT> denies git ops)
