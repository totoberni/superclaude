---
name: hpc
description: "HPC workflow: generate SLURM scripts, rsync, parse job output."
category: domain
user-invocable: true
argument-hint: "<job|sync|status|env> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# HPC Workflow

Generate SLURM scripts, rsync commands, parse job output, and display HPC environment info.

**Usage**: `/hpc <subcommand> [args]`

## Cluster Profiles

SSH aliases defined in `~/.ssh/config`. Never hardcode hostnames — read the config.

| Cluster | SSH Alias | Purpose | GPUs | CPU |
|---------|-----------|---------|------|-----|
| <Cluster> | `<cluster>` | GPU compute (<PROJECT>, ML) | A100, 1080Ti | AMD EPYC |
| <cluster-cpu> | `example-hpc` | CPU-only (example-tool sims, parallel batch) | None | example-cpu (192 cores/node) |
| EDA | `eda` | Synopsys DC synthesis (<PROJECT> project) | None | — |

### <cluster-cpu>

| Property | Value |
|----------|-------|
| Partitions | `batch` (134 nodes, 192 cores, 750GB RAM), `highmem` (4 nodes, 192 cores, 3TB RAM) |
| Max walltime | 72h |
| Interconnect | HDR 100 InfiniBand |
| Storage | `$HOME` (quota), `/scratch/$USER` (temp, no backup) |
| Internet | **None on compute nodes** |

### <Cluster>

| Property | Value |
|----------|-------|
| Max walltime | 72h (batch), 48h (gpu) |
| Storage | `$HOME` (quota), `/scratch/$USER` (temp, no backup) |
| Internet | **None on compute nodes** — pip install on login node |
| SSH note | Use `<cluster>-gpu1`/`gpu2` login nodes for SLURM GPU jobs |

**GPU partitions** (use `--gres=gpu:N`):

| Partition | GPUs/Node | CPU | RAM | Access |
|-----------|-----------|-----|-----|--------|
| `a100` | 2x A100 (NVLink) | 2x24-core Xeon 6336Y | 512GB | Staff + PGR |
| `swarm_a100` | 4x A100 (NVLink) | 2x24-core EPYC 7413 | 1TB | ECS only |
| `swarm_h100` | 8x H100 (NVSwitch) | 2x48-core Xeon 8468 | 2TB | ECS only |
| `scavenger_4a100` | 4x A100 | (as swarm_a100) | 1TB | All users (preemptible) |
| `scavenger_8h100` | 8x H100 | (as swarm_h100) | 2TB | All users (preemptible) |
| `gtx1080` | 4x GTX 1080 Ti | 28 cores | 128GB | All users |

Note: `gpu`/`gtx1080` allocate the **entire node** — submit multiple single-GPU jobs via job arrays to avoid wasting GPUs.

**Shared**: Both clusters use SLURM, `module load` (Lmod), two-phase workflow (prep on login, execute on compute). Use `module spider <name>` to search available modules. Check queue with `squeue -u $USER`.

## Compilers & Key Modules

| Software | <cluster-cpu> | <Cluster> |
|----------|----------|----------|
| GCC | 13.2.0 (D), 12.1.0, 10.3.0 | 14.1.0 (D), 13.2.0 |
| Intel | 2024.1.0 (D), 2023.2.0 | 2023.0.0, 2020.4.304 |
| Nvidia HPC | — | nvhpc (CUDA Fortran, OpenACC) |
| CUDA | — | 12.1, 11.8 |
| Python | — | 3.11, 3.10 |
| AOCC (AMD) | 4.2 | — |

`(D)` = default version. Load with `module load <name>/<version>`.

## GPU Job Tips

- **Request GPUs**: `#SBATCH --gres=gpu:N` (required — omitting causes `QOSMinGres` block)
- **No `libcuda.so.1` on login nodes**: compile/test in batch jobs or `sinteractive -p a100 --gres=gpu:1`
- **PyTorch DDP**: use `torchrun` with `--nproc_per_node=N` matching `--gres=gpu:N`
- **ECS students**: prefer `swarm_a100`/`swarm_h100` (dedicated), fall back to `scavenger_*` (preemptible)

## Subcommands

### `job` — Generate SLURM submission script

**Args**: `job <script> [--cluster <cluster>|example-hpc|generic] [--partition <name>] [--gpus <N>] [--time HH:MM:SS] [--mem <size>] [--name <jobname>]`

Auto-detect cluster from context. <cluster-cpu> jobs omit `--gres=gpu`. Template:

```bash
#!/bin/bash
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --nodes=<N> --ntasks-per-node=<T>
#SBATCH --time=<time> --mem=<mem>
#SBATCH --output=slurm-%j.out --error=slurm-%j.err

module purge
module load <modules>
source $HOME/.venv/bin/activate
cd $SLURM_SUBMIT_DIR
<command>
```

Write to `$PROJECT/job_<name>.sh`. Do NOT execute.

### `sync` — Generate rsync command

**Args**: `sync <up|down> [--cluster <cluster>|example-hpc] [--exclude <pattern>...]`

Read SSH alias from `~/.ssh/config`. Default remote base: `/scratch/$USER/`.

```bash
# UP: rsync -avz --progress --exclude '.git' --exclude '__pycache__' ...
# DOWN: rsync -avz --progress --include 'results/***' --include 'slurm-*' ...
```

**Output only** — never execute SSH or rsync.

### `status` — Parse SLURM output files

**Args**: `status [--job <slurm-ID>] [--project <path>]`

Find `slurm-*.out`/`.err`, parse: start/end time, exit status, GPU util, training progress, errors. Summary table:

```
| Job ID | Status | Runtime | GPU | Final Metric | Error |
```

### `env` — Display HPC environment info

**Args**: `env [--cluster <cluster>|example-hpc|generic]`

Display partitions, modules, storage, and tips for the target cluster.

## Constraints

- Never execute SSH or rsync — only generate commands
- Never submit jobs — only generate scripts
- All file paths absolute
- Read `~/.ssh/config` for hostnames (DRY — single source of truth)
- Respect two-phase HPC workflow: prepare on login node, execute on compute
