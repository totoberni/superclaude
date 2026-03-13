---
name: handoff
description: "Creates a structured handoff document for agent-to-agent or session-to-session transitions."
user-invocable: true
disable-model-invocation: true
argument-hint: "<target-agent-or-session>"
allowed-tools: Read, Write, Bash
---

# Handoff Protocol

Create a structured handoff for: $ARGUMENTS

## Steps

1. Summarize current session state:
   - What was the objective?
   - What has been completed?
   - What remains?
2. List all files modified in this session: `git diff --name-only`
3. List key decisions made
4. List any blockers or open questions
5. Determine handoff location:
   - **If superclaude orch**: Write bootstrap to `~/.claude/comms/<target-orch>/bootstrap.md` (Meta writes this)
   - **If in-project**: Write to `.orchestrator/handoff-$(date +%Y%m%d-%H%M).md`
   - **If session-to-session**: Present in chat (don't write a file)

## Handoff Template

```markdown
# Handoff to $ARGUMENTS

**Date**: [timestamp]
**From**: Current session
**To**: $ARGUMENTS

## Objective
[What was being worked on]

## Completed
- [x] [completed items]

## Remaining
- [ ] [remaining items]

## Files Modified
[list from git diff --name-only]

## Key Decisions
- [decision 1]

## Blockers / Open Questions
- [if any]

## Relevant Memory
- Project memory: `~/.claude/agent-memory/shared/projects/<project>.md`
- Plan: `~/.claude/plans/<plan>/plan.md`
- State: `~/.claude/plans/<plan>/state*.md`

## Context
[Any critical context the next agent/session needs]
```
