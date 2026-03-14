---
name: test-infra
description: "Run infrastructure regression tests. --quick for fast subset."
category: meta
user-invocable: true
disable-model-invocation: true
argument-hint: "[--quick | --component <name>]"
allowed-tools: Read, Bash
---

# Test-Infra — Infrastructure Regression Suite

Run the regression test suite and display results. Suggests fixes on failure.

**Arguments**: $ARGUMENTS — optional flags

## Steps

1. Run the test suite:
   ```bash
   bash $HOME/.claude/scripts/infra-test.sh $ARGUMENTS
   ```
   Default runs `--full` (all categories). Pass `--quick` for hooks+settings only, or `--component <name>` for a single category.

2. Display the output directly.

3. If any `FAIL` lines appear, suggest fixes:
   - **ST (settings)**: JSON parse → `jq .`, deny floor → check for deleted deny rules
   - **H (hooks)**: module syntax → `bash -n` on failing module, test suite failure → run `test-hooks.sh` standalone
   - **A (agents)**: frontmatter → add `---` delimiters, model → use opus/sonnet/haiku, symlinks → check target exists, skill refs → verify skill dir
   - **S (skills)**: missing SKILL.md → create it, tool names → use Read/Write/Edit/Bash/Glob/Grep/Agent
   - **R (rules)**: heading → start with `# Title`, numbering → rename file prefix
   - **C (comms)**: missing files → create 4-file set, agent xref → verify agent definition exists

## Components

| Flag | Tests | Time |
|------|-------|------|
| (none) / `--full` | All: ST + H + A + S + R + C | ~10s |
| `--quick` | H + ST (hooks + settings) | ~2s |
| `--component agents` | A only | <1s |
| `--component skills` | S only | <1s |
| `--component rules` | R only | <1s |
| `--component comms` | C only | <1s |
