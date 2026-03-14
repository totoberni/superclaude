---
name: research
description: "ML research: find implementations, design ablations, check params."
category: domain
user-invocable: true
argument-hint: "<paper|ablation|hyperparams|reproduce> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# ML Research Assistant

Tools for ML research workflows: paper implementation search, ablation design, hyperparameter analysis, reproduction fidelity checking.

**Usage**: `/research <subcommand> [args]`

## Project Detection

Detect the ML project from `$ARGUMENTS` or infer from context:
1. Explicit path or project name in args
2. Infer from recent conversation context
3. If ambiguous, ask the user

Set `PROJECT` to the absolute project path. Check `~/.claude/agent-memory/shared/projects/<project>.md` for project-specific context.

## Subcommands

### `paper` — Find implementation of a paper

**Args**: `paper "<title or key phrase>" [--in <project-path>]`

Search the project codebase for implementations of concepts from a specific paper:

1. Parse the paper title/phrase from args
2. Search the project for related implementations:
   - Grep for paper title, author names, arxiv IDs in comments and docstrings
   - Grep for key method names (e.g., "rotary embedding", "flash attention", "mixture of experts")
   - Search config files for model variant names
3. For each match, read the file and identify:
   - Which paper concepts are implemented
   - How faithfully they follow the paper
   - Any deviations or modifications noted in comments
4. Output a mapping:

```
## Paper: "<title>"

### Implementations Found
| File | Lines | Concept | Faithful? | Notes |
|------|-------|---------|-----------|-------|

### Not Found
- [list concepts from the paper not found in code]
```

### `ablation` — Design an ablation study

**Args**: `ablation <component> [--baseline <EXP-ID>] [--budget <N>]`

Design a systematic ablation study for a model component:

1. Read the project codebase to understand the target component
2. Read `$PROJECT/experiments.md` if it exists (for baseline context)
3. Identify the component's parameters and sub-components
4. Design ablation experiments:

```
## Ablation Study: <component>

### Baseline
- Config: <baseline config>
- Key metric: <from experiments.md or specified>

### Ablation Plan
| # | What to Remove/Change | Hypothesis | Config Change | Priority |
|---|----------------------|------------|---------------|----------|
| 1 | Remove <sub-component> | Tests if X contributes to Y | key: value | High |
| 2 | Replace <A> with <B> | Tests alternative approach | key: value | Medium |

### Estimated Compute
- Total runs: N
- Est. time per run: (based on baseline if available)
- Total GPU-hours: ~X
```

Budget (default 5) limits the number of proposed ablation runs. Prioritize by expected information gain.

### `hyperparams` — Compare against paper recommendations

**Args**: `hyperparams [--paper "<title>"] [--config <path>]`

Compare current project hyperparameters against paper recommendations:

1. Read the project's config files (search for `config.py`, `config.yaml`, `*.json` configs, argparse defaults)
2. Extract current hyperparameters (learning rate, batch size, optimizer, scheduler, etc.)
3. If `--paper` specified, compare against known paper defaults:
   - Common baselines: GPT-2/3 (Radford et al.), BERT (Devlin et al.), ViT (Dosovitskiy et al.)
   - Search code comments for paper-referenced values
4. Output comparison:

```
## Hyperparameter Comparison

| Parameter | Current | Paper | Match? | Notes |
|-----------|---------|-------|--------|-------|
| lr        | 3e-4    | 6e-4  | Diff   | Paper uses 2x for larger batch |
| batch     | 32      | 64    | Diff   | May need to scale lr |
| warmup    | 1000    | 2000  | Diff   | Paper trains longer |

### Recommendations
- [actionable suggestions based on discrepancies]
```

### `reproduce` — Check reproduction fidelity

**Args**: `reproduce <paper-title-or-path> [--strict]`

Check how faithfully the current project reproduces a paper's methodology:

1. Read the project's model architecture, training loop, data pipeline
2. Build a checklist of reproduction requirements:

```
## Reproduction Fidelity: "<paper>"

### Architecture
| Component | Paper | Implementation | Match? |
|-----------|-------|----------------|--------|

### Training
| Setting | Paper | Implementation | Match? |
|---------|-------|----------------|--------|

### Data
| Aspect | Paper | Implementation | Match? |
|--------|-------|----------------|--------|

### Overall Fidelity
- Architecture: X/Y components match
- Training: X/Y settings match
- Data: X/Y aspects match

### Critical Gaps
- [list any gaps that would prevent reproduction]

### Acceptable Deviations
- [list deviations with justification, e.g., smaller dataset for compute budget]
```

If `--strict`, flag ALL deviations. Default mode only flags critical gaps.

## Constraints

- Works for any ML project, not just example-project
- File-based analysis only — never import or run ML code
- All paths absolute
- Respect project-specific git restrictions
