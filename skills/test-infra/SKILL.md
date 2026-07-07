---
name: test-infra
description: "Run infrastructure regression tests. --quick for fast subset."
category: meta
user-invocable: true
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
   - **A (agents)**: frontmatter → add `---` delimiters, model → use opus/sonnet/haiku, symlinks → check target exists, skill refs → verify skill dir; **A6 grants** → restore SendMessage/Skill/WebSearch/WebFetch on meta.md, SendMessage/Skill on orch.md; **A7 fleet** → each w-*.md needs exactly one `## Report Contract (wf-skills)` section, Skill on the 9 reasoning workers, absent from w-committer/w-explorer
   - **S (skills)**: missing SKILL.md → create it, tool names → use Read/Write/Edit/Bash/Glob/Grep/Agent; **S6 _shared** → restore the 8 rubric blocks with a `Consumed by:` line, no em/en-dash; **S7 new skills** → converge/review-dispatch need name/description/user-invocable and no `disable-model-invocation`
   - **R (rules)**: heading → start with `# Title`, numbering → rename file prefix
   - **C (comms)**: missing files → create 4-file set, agent xref → verify agent definition exists
   - **WF (wf-skills scripts)**: missing/non-exec/`bash -n` → check `scripts/comms/*.sh`, `scripts/decontaminate.sh`, `scripts/swarm/recover-worker.sh`; behavioral → broker-queries refuses unknown verbs (exit 1), decontaminate flags forbidden tokens (exit 1) and passes clean files (exit 0)

## Components

| Flag | Tests | Time |
|------|-------|------|
| (none) / `--full` | All: ST + H + A + S + R + C + M + SK + WF | ~10s |
| `--quick` | H + ST (hooks + settings) | ~2s |
| `--component agents` | A only (incl. A6 grants, A7 fleet contract) | <1s |
| `--component skills` | S only (incl. S6 _shared, S7 converge/review-dispatch) | <1s |
| `--component rules` | R only | <1s |
| `--component comms` | C only | <1s |
| `--component wfscripts` | WF only (comms/swarm/decontaminate scripts) | <1s |
