---
name: experiment-harness
description: "Multi-seed experiment runner + passport.yaml claim-provenance verifier."
category: domain
user-invocable: true
disable-model-invocation: true
argument-hint: "<run|verify> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Experiment Harness

Run a target script across N seeds, aggregate `mean +/- std` (min/max/n), and bind
each reported number to its provenance in a `passport.yaml` so unprovenanced or
stale claims can be blocked.

Distinct from `/experiment` (a markdown `experiments.md` tracker — no imports). This
is an executable Python tool for numeric aggregation + provenance verification.

**Engine**: `~/.claude/scripts/experiment-harness.py` — run with the venv python:

```bash
~/.claude/.venv/bin/python ~/.claude/scripts/experiment-harness.py <run|verify> ...
```

## `run` — multi-seed aggregate

```bash
~/.claude/.venv/bin/python ~/.claude/scripts/experiment-harness.py \
  run --script train.py --seeds 1,2,3 \
      --metric test_acc \
      --claim-id table2_acc --passport passport.yaml
```

- `--script CMD...` — a lone `.py` runs under the venv python; extra tokens are
  passed as args. Each run gets `--seed N` appended AND `EXPERIMENT_SEED=N` exported.
- The target prints results as a bare number, `key=value` lines, or a flat JSON
  object. The harness parses, then aggregates per metric.
- `--seeds` — `1,2,3` or inclusive range `1-5`.
- `--metric` — pin one metric when several are emitted (required to back a claim).
- `--claim-id` + `--passport` — record/update a provenance entry (deterministic;
  re-running the same seeds reproduces the same aggregate).

## `verify` — block unprovenanced / stale claims

```bash
~/.claude/.venv/bin/python ~/.claude/scripts/experiment-harness.py \
  verify --passport passport.yaml --claims claims.txt [--strict]
```

- `--claims` — `.txt` (one `<claim_id> [value] [std]` per line, `#` comments) or
  `.yaml` (list / mapping / passport-shaped).
- Flags `MISSING` (no passport entry) and `STALE` (recorded value/std drifted).
- Default exits 0 (reporting). `--strict` exits 2 on any finding — for CI / pre-commit
  gates. Exit 1 is reserved for the tool's own errors (bad args, I/O, target crash).

## passport.yaml schema

`claims: { <claim_id>: { experiment, script, seeds, git_commit, date, [metric],
value, std, n } }`. Full schema + worked example:
`~/.claude/scripts/passport.example.yaml` (also summarised in the script header).

## Constraints

- All paths absolute. Run via `~/.claude/.venv/bin/python` (PyYAML lives there).
- Reporting tool: non-zero exit only on its own error (exit 2 = intentional
  `--strict` block, not a crash).
