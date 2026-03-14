---
name: good-idea
description: "Record effective solutions and patterns for reuse across sessions."
category: memory
user-invocable: true
disable-model-invocation: true
argument-hint: "[project-name or 'all']"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Good Idea Retrospective

Run a post-session retrospective to identify and record effective solutions, patterns, and wins.

**Scope**: $ARGUMENTS (project name, or "all" for cross-project patterns)

## Procedure

### 0. Agent Class Detection

Determine your agent class for dual-write:

1. Infer class from your agent name:
   - Names starting with `o-` or `orch` → class `orch`
   - `scaf` or `scaffolder` → class `scaf`
   - `meta` → class `meta`
   - `w-<type>` prefix → class `w-<type>` (e.g., `w-debugger-1` → `w-debugger`)
2. Check if `~/.claude/agent-memory/class/<class>/mtm.md` exists
   - **Yes**: dual-write enabled — class-applicable wins go here too
   - **No**: primary write only (don't create class dirs)

### 1. Gather Evidence

Read the session's work by checking (in parallel where possible):
- `git -C <repo> log --oneline -20` — recent commits (look for clean merges, elegant solutions, efficient workflows)
- `git -C <repo> diff --stat HEAD~5..HEAD` — scope of changes (look for high-impact, low-diff solutions)
- Any successful outputs from the current conversation
- Orch reports — scan `~/.claude/comms/*/reports.md` for any orch directories that exist (look for DONE status, smooth completions)
- Plan state — scan `~/.claude/plans/*/state*.md` (look for tasks completed faster than expected)

### 2. Classify Learning Type

For every win found, assign a learning type tag:

| Type | When to Use | Example |
|------|-------------|---------|
| `[WORKING_SOLUTION]` | Confirmed working command, pattern, or approach | `git -C` avoiding cd into project dirs |
| `[DECISION]` | Design choice with reasoning and alternatives | Dependency-ordered merges over alphabetical |
| `[PREFERENCE]` | User/project preference (style, tooling, conventions) | Always use `const`/`let` in Node.js, `var` in GAS |

Include the type as a tag prefix in the Phase column of the win table:

```markdown
| W-N | [WORKING_SOLUTION] P2 | What worked | Why it worked | Reusable? |
| W-N | [DECISION] P1 | Design choice | Reasoning + alternatives | Reusable? |
| W-N | [PREFERENCE] P0 | Convention | Why this preference | Reusable? |
```

Existing entries without tags remain valid — tags are additive.

### 3. Identify Wins

Look for patterns in these categories:

| Category | What to Look For | Example |
|----------|-----------------|---------|
| **Tool usage** | A tool/flag/workflow that saved significant time | `git -C` avoiding cd; merge-resolver for bulk conflicts |
| **Architecture** | A design decision that made later work easier | Dependency-ordered merges reducing cascading conflicts |
| **Delegation** | An agent task description that produced clean results | Self-contained worker prompts with explicit file scopes |
| **Process** | A workflow step that prevented problems | Stashing WSL changes before merges |
| **Code pattern** | A code structure that was clean and reusable | Config-driven provider architecture |

### 4. Classify Scope

| Category | Scope | Storage Location |
|----------|-------|------------------|
| **Project win** | One project | `~/.claude/agent-memory/shared/projects/<project>.md` → Wins table |
| **Cross-project win** | 2+ projects | `~/.claude/agent-memory/shared/global/ltm.md` → Index |
| **Class-level win** | Applies as this agent class on any project | `~/.claude/agent-memory/class/<class>/mtm.md` → Wins table |
| **Promotable pattern** | All agents, all projects | `~/.claude/rules/20-tool-conventions.md` (if tool-related) |

**Important**: Project wins go to `shared/projects/`, NOT to any orch-specific memory. This ensures all orch instances (current and future) working on the same project can see past wins.

### 5. Check for Duplicates

Before recording, search existing files for the same pattern:
- Read `~/.claude/agent-memory/shared/projects/<project>.md` — already recorded?
- Read `~/.claude/agent-memory/shared/global/ltm.md` — already a cross-project win?
- Read `~/.claude/agent-memory/class/<class>/mtm.md` — already a class-level win?
- Read `~/.claude/rules/20-tool-conventions.md` — already a rule?

If already recorded, skip. Don't duplicate.

### 6. Record

**For project wins** — append to `~/.claude/agent-memory/shared/projects/<project>.md` Wins table:

```markdown
| W-<N> | <Phase> | <What Worked> | <Why It Worked> | <Reusable? + scope> |
```

If the project file doesn't exist yet, create it with the standard template:

```markdown
# <Project Name> — Agent Memory

## Wins

| # | Phase | What Worked | Why It Worked | Reusable? |
|---|-------|-------------|---------------|-----------|

## Mistakes

| # | Phase | What Went Wrong | Root Cause | Fix Applied | Prevention Rule | Occurrences |
|---|-------|----------------|------------|-------------|-----------------|-------------|

## Gotchas (project-specific)

(none yet)
```

**For cross-project wins** — append to `~/.claude/agent-memory/shared/global/ltm.md` Index:

```markdown
| CW-<N> | <Pattern> | <Source Projects> | No |
```

**Dual-write to class memory** (secondary, additive) — for each win, ask: "Is this specific to this project's codebase, or would it help any agent of my class on any project?" If class-applicable AND dual-write is enabled (Step 0), append to `~/.claude/agent-memory/class/<class>/mtm.md` Wins table:

```markdown
| <next-ID> | <Pattern> | <One-liner> [<project>] | <Source ref> |
```

- Tag with `[<project>]` to track origin
- If `--universal` flag was passed, prefix the one-liner with `[PROMOTE]` — signals v3 /lt-mem for promotion to global LTM
- If class dir doesn't exist, skip secondary write silently

### 7. Check Promotion Threshold

For any project win marked `Reusable? = Yes` and seen in 2+ projects:
1. Add to `~/.claude/agent-memory/shared/global/ltm.md` as a cross-project win
2. If it's a tool pattern, promote to `~/.claude/rules/20-tool-conventions.md`
3. Mark as `Promoted` in the source project files

### 8. Report

Present a summary table:

```
## Good Ideas Summary

| # | Win | Category | Recorded In | Reusable? | Promoted? |
|---|-----|----------|-------------|-----------|-----------|
| 1 | ... | project / cross-project | file path | yes/no | yes/no |

### Patterns Promoted to Rules
- [list any newly promoted patterns, or "None"]

### Reusable Patterns (not yet promoted)
- [list patterns marked reusable but only seen in 1 project]
```

## Memory Locations

- **Project**: `~/.claude/agent-memory/shared/projects/<project>.md` (primary)
- **Class**: `~/.claude/agent-memory/class/<class>/mtm.md` (secondary, dual-write)
- **Cross-project**: `~/.claude/agent-memory/shared/global/ltm.md`
- **Tool patterns**: `~/.claude/rules/20-tool-conventions.md` (promotable)
- Write scopes: rule 12. Class writes are layer 2 only — never write to `shared/global/ltm.md` (reserved for v3 /lt-mem).
