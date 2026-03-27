---
name: research
description: "ML research: find implementations, design ablations, check params."
category: domain
user-invocable: true
disable-model-invocation: true
argument-hint: "<paper|ablation|hyperparams|reproduce> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# ML Research Assistant

**Usage**: `/research <subcommand> [args]`

Detect project from args or conversation context. Check `shared/projects/<project>.md` for existing context.

## `paper` — Find implementation of a paper

**Args**: `paper "<title>" [--in <path>]`

1. Grep project for paper title, author names, arxiv IDs, key method names
2. For each match: which concepts implemented, how faithfully, deviations noted
3. Output: `| File | Lines | Concept | Faithful? | Notes |` + "Not Found" list

## `ablation` — Design ablation study

**Args**: `ablation <component> [--baseline <EXP-ID>] [--budget <N>]`

1. Read component code + `experiments.md` for baseline
2. Identify parameters and sub-components
3. Output: `| # | What to Change | Hypothesis | Config Change | Priority |`
4. Budget (default 5) limits runs. Prioritize by information gain.
5. Include compute estimate (runs × time per run)

## `hyperparams` — Compare against paper

**Args**: `hyperparams [--paper "<title>"] [--config <path>]`

1. Extract current hyperparams from config files (config.py, yaml, json, argparse)
2. Compare against paper defaults
3. Output: `| Parameter | Current | Paper | Match? | Notes |` + recommendations

## `reproduce` — Check reproduction fidelity

**Args**: `reproduce <paper> [--strict]`

1. Read architecture, training loop, data pipeline
2. Build checklist: Architecture (`| Component | Paper | Impl | Match? |`), Training, Data
3. Score: X/Y match per category
4. Flag critical gaps + acceptable deviations
5. `--strict` flags ALL deviations; default only critical gaps
6. **Divergence log**: for each deviation found, check if a `docs/reprod-notes.md` (or equivalent) exists in the project. If yes, cross-reference. If no, recommend creating one. Each entry needs: what changed, why (hardware constraint / missing artifact / intentional extension), and impact assessment (none / negligible / measured). This living document prevents re-discovery of known deviations and gives reviewers a single place to audit reproduction fidelity

## Constraints

- File-based analysis only — never import/run ML code
- All paths absolute, respect git restrictions
