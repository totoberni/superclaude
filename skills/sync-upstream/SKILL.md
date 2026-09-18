---
name: sync-upstream
description: "Use when syncing the upstream Claude Code reference library and diffing"
category: meta
user-invocable: true
argument-hint: "[audit]"
allowed-tools: Read, Bash, Glob, Grep
---

# Sync Upstream — Reference Library Update

Sync and analyze the upstream Claude Code community reference at `~/.claude/upstream/awesome-claude-code/`.

**Arguments**: $ARGUMENTS — empty for pull+report, `audit` for full comparison.

## Default Mode (`/sync-upstream`)

Pull latest and report what's new and relevant.

### Steps

1. Pull latest from upstream (no `cd`; use `git -C`):
   ```
   git -C ~/.claude/upstream/awesome-claude-code pull --ff-only
   ```
   If the fast-forward fails because the clone diverged, run `git -C ~/.claude/upstream/awesome-claude-code fetch origin` followed by `git -C ~/.claude/upstream/awesome-claude-code reset --hard origin/main` (the clone is a disposable read-only mirror).
   If offline (git fails), report "Offline — using cached reference" and continue with local copy.

2. Size the resource catalog: use Grep in `count` mode on `~/.claude/upstream/awesome-claude-code/THE_RESOURCES_TABLE.csv` (pattern `.`) to get the number of rows.

3. Search for entries relevant to our infrastructure. Use Grep with targeted patterns:
   - `hook` — hook patterns and implementations
   - `skill|command|slash` — slash command ideas
   - `agent` — agent configurations
   - `workflow|automation` — workflow patterns
   - `config|settings` — settings.json patterns
   - `security|permission` — security patterns

4. Report to the user:
   ```
   [SYNC] Upstream Reference Updated
   ──────────────────────────────────
   Last pulled: <date>
   Total resources: <N>

   Relevant to our infrastructure:
   - <resource> (<category>) — could enhance <component>
   - ...
   ```

## Audit Mode (`/sync-upstream audit`)

Full comparison of our infrastructure against the upstream catalog.

### Steps

1. Pull latest (same as default mode step 1)

2. Inventory what we HAVE, with the Glob tool (count the matches of each pattern):
   - Hook handlers: `~/.claude/hooks/sc_hooks/handlers/*.py`
   - Skills: `~/.claude/skills/*/SKILL.md`
   - Agents: `~/.claude/agents/*.md`
   - Rules: `~/.claude/rules/*.md`
   - Scripts: `~/.claude/scripts/**/*.py`

3. Read the upstream catalog (THE_RESOURCES_TABLE.csv) — use targeted Grep, not full read:
   - Search for categories: hooks, commands, agents, configuration, workflows
   - For each category, list entries and check if we have an equivalent

4. Identify **gaps** — patterns the community has that we don't
5. Identify **opportunities** — features that could improve our workflow
6. Present as actionable recommendations with effort estimates (small/medium/large)

## Constraints

- The upstream clone is **READ-ONLY** — never modify files in `~/.claude/upstream/`
- This skill is **informational** — report but do NOT auto-implement features
- Implementation decisions go through Meta, which dispatches workers after owner approval
- Don't read the entire CSV — use targeted grep for relevant categories
- If the upstream dir doesn't exist, tell the user to run: `git clone --depth 1 https://github.com/hesreallyhim/awesome-claude-code.git ~/.claude/upstream/awesome-claude-code`
