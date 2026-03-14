---
name: hpc
description: "HPC workflow: generate SLURM scripts, rsync, parse job output."
category: domain
user-invocable: true
disable-model-invocation: true
argument-hint: "<job|sync|status|env> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# HPC Workflow

Generate SLURM scripts, rsync commands, parse job output, and display HPC environment info.

**Usage**: `/hpc <subcommand> [args]`

## Cluster Profiles

### cluster (University of UNIVERSITY)

| Property | Value |
|----------|-------|
| Scheduler | SLURM |
| Partitions | `batch`, `gpu`, `gtx1080`, `a100` |
| Module system | `module load` (Lmod) |
| Storage | `$HOME` (quota), `/scratch/$USER` (temp, no backup) |
| Internet | **None on compute nodes** — all downloads must happen on login node |
| GPU types | GTX 1080 Ti, A100 |
| Max walltime | 72h (batch), 48h (gpu) |

### Generic SLURM

Fallback profile for any SLURM cluster. Uses conservative defaults.

## Subcommands

### `job` — Generate SLURM submission script

**Args**: `job <script.py> [--cluster example-hpc|generic] [--partition <name>] [--gpus <N>] [--time <HH:MM:SS>] [--mem <size>] [--name <jobname>]`

Generate a SLURM batch script. Detect cluster from args or project context.

**cluster template**:

```bash
#!/bin/bash
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --gres=gpu:<N>
#SBATCH --time=<time>
#SBATCH --mem=<mem>
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err

module purge
module load python/3.11
module load cuda/12.1

source $HOME/.venv/bin/activate

cd $SLURM_SUBMIT_DIR
python <script.py>
```

**Generic template**: Same structure, no module load lines. Add `# TODO: configure modules for your cluster`.

Write the script to `$PROJECT/job_<name>.sh`. Do NOT execute it.

### `sync` — Generate rsync command

**Args**: `sync <direction> [--cluster example-hpc|generic] [--host <hostname>] [--exclude <pattern>...]`

Directions:
- `up`: local → remote (push code/data to cluster)
- `down`: remote → local (pull results from cluster)

Generate the rsync command with sensible defaults:

```bash
# UP: push to cluster
rsync -avz --progress \
  --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.venv' --exclude 'wandb/' --exclude '*.pt' \
  <local_path>/ <user>@<host>:<remote_path>/

# DOWN: pull results
rsync -avz --progress \
  --include 'results/***' --include 'slurm-*.out' --include 'slurm-*.err' \
  --include 'summary.json' --exclude '*' \
  <user>@<host>:<remote_path>/ <local_path>/results/
```

**Output the command only** — do NOT execute SSH or rsync. Print with explanation of what it will do.

For cluster: default host is `example-hpc5.university.ac.uk`, remote base is `/scratch/$USER/`.

### `status` — Parse SLURM output files

**Args**: `status [--job <slurm-ID>] [--project <path>]`

1. Find SLURM output files: `$PROJECT/slurm-*.out` and `$PROJECT/slurm-*.err`
2. Parse each output file for:
   - Job start/end time (from SLURM header lines)
   - Exit status (success/failure/OOM/timeout)
   - GPU utilization (if `nvidia-smi` output present)
   - Training progress (epoch/step counts, loss values from last N lines)
   - Error messages (scan .err files, last 20 lines of .out for tracebacks)
3. If `--job` specified, show only that job's details
4. Summary table:

```
| Job ID | Status | Runtime | GPU | Final Metric | Error |
```

Handle missing files gracefully — report "No SLURM output files found."

### `env` — Display HPC environment info

**Args**: `env [--cluster example-hpc|generic]`

Display reference information for the target cluster:

```
## HPC Environment: cluster

### Available Partitions
| Partition | Max Nodes | Max Time | GPUs |
|-----------|-----------|----------|------|

### Key Modules
- python/3.11, python/3.10
- cuda/12.1, cuda/11.8
- gcc/12.2

### Storage
- $HOME: 50GB quota, backed up
- /scratch/$USER: 1TB temp, NOT backed up, 30-day purge

### Tips
- No internet on compute nodes — pip install on login node
- Use `module spider <name>` to search available modules
- Use `squeue -u $USER` to check job queue
```

## Constraints

- Never execute SSH or rsync — only generate commands
- Never submit jobs — only generate scripts
- All file paths absolute
- Support cluster (UNIVERSITY) + generic SLURM
- Respect two-phase HPC workflow: prepare on login node, execute on compute
