---
name: nb-monitor
description: "Run a notebook with live per-cell progress + BROKEN/HUNG/SLOW state."
category: code-quality
user-invocable: true
argument-hint: "run NOTEBOOK [--cell-timeout 300] [--kernel-name NAME] [--progress-path PATH] [--heartbeat 0.5] [--allow-errors]"
allowed-tools: Bash, Read
---

# Notebook Runtime Monitor

Executes a Jupyter notebook through `nbclient`'s `NotebookClient` with per-cell
hooks and streams live progress to `~/.claude/.nb-progress.json` (schema 1).
A statusline reader polls that file (~1 s) and renders the run state — this
skill is the **producer / single writer**. It does NOT mutate the notebook
(no output is written back); it is a read-execute monitor only.

This is a **separate, thin** tool — it does not touch the larger `/notebook`
skill or its `nb_*.py` files.

## What it surfaces

| Field | Meaning |
|-------|---------|
| `current_index` / `total_cells` / `current_label` | cell N of total (0-based index + `"N/total"` or first cell tag) |
| `cell_elapsed_s` | wall seconds on the current cell (heartbeat-updated during long cells) |
| `last_output_line` | last stdout fragment of the live cell — the **tqdm relay** (CR-redraw aware) |
| `status` | `idle` \| `running` \| `done` \| `broken` \| `hung` |
| `kernel` | `alive` \| `dead` |
| `error` | short summary when `broken`; `null` otherwise |
| `updated` | unix-epoch float written every update — the consumer uses it for staleness/HUNG |

## Run-state classification

- **BROKEN** — a cell raised. nbclient surfaces `CellExecutionError`;
  `status="broken"`, `error="<ename>: <evalue>"`, `kernel="alive"`, exit `1`.
- **HUNG** — the current cell exceeded `--cell-timeout` (`CellTimeoutError`) or
  the kernel died (`DeadKernelError`); `status="hung"`, `kernel="dead"`, exit `2`.
- **SLOW-but-alive** — a cell runs long but its stdout keeps advancing. The
  monitor stays `status="running"` and keeps `last_output_line` + `updated`
  fresh, so the consumer (which flags HUNG by `updated` staleness) sees the run
  is alive. Only the hard `--cell-timeout` converts a stuck cell into HUNG.

## Usage

Run via the dedicated superclaude venv (absolute path — venv discipline). The
notebook needs a launchable kernelspec; pass `--kernel-name` to override the
one recorded in the notebook metadata.

```bash
# Monitor a run with the default 300 s per-cell hang timeout.
~/.claude/.venv/bin/python ~/.claude/scripts/nb-monitor.py run analysis.ipynb

# Tighter hang detection + explicit kernel + faster heartbeat.
~/.claude/.venv/bin/python ~/.claude/scripts/nb-monitor.py run analysis.ipynb \
    --cell-timeout 120 --kernel-name python3 --heartbeat 0.5

# Poll the live state from a statusline / another shell.
cat ~/.claude/.nb-progress.json
```

**Args**: subcommand `run` + positional `NOTEBOOK`; `--cell-timeout` (seconds,
default `300`; exceeding it = HUNG); `--kernel-name` (kernelspec, default = the
notebook's own); `--progress-path` (default `~/.claude/.nb-progress.json`);
`--heartbeat` (seconds between live updates during a cell, default `0.5`);
`--allow-errors` (continue past cell errors instead of marking BROKEN).

**Exit codes**: `0` done · `1` BROKEN · `2` HUNG · `3` usage/load error
(bad path or missing dep, surfaced before any execution).

## Output contract

The `.nb-progress.json` write is atomic (temp file + `os.replace`), so a reader
never observes a half-written or corrupt file, and a monitor crash leaves the
last complete snapshot intact. The schema is fixed at `"schema": 1`.

## Dependencies

Needs `nbclient` + `nbformat` (listed in `~/.claude/dependencies.yml`) plus a
launchable Jupyter kernel (`ipykernel`) for the target notebook. Missing deps
exit `3` with a message; agents do not pip-install (owner owns dependencies.yml).
