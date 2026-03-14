---
name: health
description: "Infrastructure health check. /health [component] for specific."
category: health
user-invocable: true
argument-hint: "[component]"
allowed-tools: Read, Bash, Glob
---

# Health — Infrastructure Health Check

Run the infrastructure health check script and display results.

**Arguments**: $ARGUMENTS — optional component name

## Steps

1. Run the backing script:
   ```bash
   bash $HOME/.claude/scripts/infra-health.sh $ARGUMENTS
   ```
   If $ARGUMENTS is empty, runs a full audit. If a component name is provided (settings, hooks, agents, comms, sessions, memory), runs only that check.

2. Display the output directly to the user.

3. If any lines contain `❌` (failure), suggest specific remediation steps based on the failure type:
   - **settings.json parse failure**: backup, fix JSON syntax, validate with `jq .`
   - **Hook syntax error**: run `bash -n <hook>` to locate the error, fix, re-validate
   - **Missing frontmatter**: add `---` delimited YAML frontmatter with required fields
   - **Missing comms files**: create the 4-file set (bootstrap.md, directives.md, escalations.md, reports.md)
   - **Dead rule globs**: the `paths:` filter matches no files — either update the glob or remove the frontmatter filter

## Valid Components

| Component | What It Checks |
|-----------|---------------|
| `settings` | JSON validity, deny rules, duplicate keys, hook registration |
| `hooks` | Syntax, exit codes, permissions, line count |
| `agents` | Frontmatter, model field, valid model values |
| `comms` | 4-file completeness, cross-references with agents |
| `sessions` | Active timers, stale PIDs, orphan files, RAM usage |
| `memory` | MEMORY.md existence, line counts, footprint |
| `rules` | Frontmatter paths: globs, dead rule detection |

## Examples

- `/health` — full audit of all components
- `/health hooks` — check only hook scripts
- `/health sessions` — check session timer state and RAM
