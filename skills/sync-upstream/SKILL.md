---
name: sync-upstream
description: "Sync upstream Claude Code reference library and diff changes."
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

1. Pull latest from upstream:
   ```bash
   cd $HOME/.claude/upstream/awesome-claude-code && \
     git pull --ff-only 2>/dev/null || \
     (git fetch origin && git reset --hard origin/main) 2>&1
   ```
   If offline (git fails), report "Offline — using cached reference" and continue with local copy.

2. Read the resource catalog:
   ```bash
   wc -l $HOME/.claude/upstream/awesome-claude-code/THE_RESOURCES_TABLE.csv
   ```

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

2. Inventory what we HAVE:
   ```bash
   echo "=== Our Infrastructure ==="
   echo "Hooks: $(ls $HOME/.claude/hooks/*.sh 2>/dev/null | wc -l)"
   echo "Skills: $(ls -d $HOME/.claude/skills/*/SKILL.md 2>/dev/null | wc -l)"
   echo "Agents: $(ls $HOME/.claude/agents/*.md 2>/dev/null | wc -l)"
   echo "Rules: $(ls $HOME/.claude/rules/*.md 2>/dev/null | wc -l)"
   echo "Scripts: $(ls $HOME/.claude/scripts/*.sh 2>/dev/null | wc -l)"
   ```

3. Read the upstream catalog (THE_RESOURCES_TABLE.csv) — use targeted Grep, not full read:
   - Search for categories: hooks, commands, agents, configuration, workflows
   - For each category, list entries and check if we have an equivalent

4. Identify **gaps** — patterns the community has that we don't
5. Identify **opportunities** — features that could improve our workflow
6. Present as actionable recommendations with effort estimates (small/medium/large)

## Constraints

- The upstream clone is **READ-ONLY** — never modify files in `~/.claude/upstream/`
- This skill is **informational** — report but do NOT auto-implement features
- Implementation decisions go through Meta -> directive -> scaf flow
- Don't read the entire CSV — use targeted grep for relevant categories
- If the upstream dir doesn't exist, tell the user to run: `git clone --depth 1 https://github.com/hesreallyhim/awesome-claude-code.git ~/.claude/upstream/awesome-claude-code`
