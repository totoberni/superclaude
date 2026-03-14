---
name: pleh
description: "Spawn parallel help. /pleh = self-help (2x). /pleh agent-name = cross-help (3x)."
category: orchestration
user-invocable: true
argument-hint: "[target-agent]"
allowed-tools: Read, Glob, Grep, Agent, Bash
---

# Pleh — Parallel Reinforcement

Spawn sub-agents to help with the current (or a target agent's) task. "pleh" = help backwards — help arrives from behind.

**Arguments**: $ARGUMENTS — optional target agent name

- If empty: **self-pleh** (2x on current task)
- If agent name provided: **cross-pleh** (3x on target's task)

## Self-Pleh (no target)

Double your throughput on the current task by spawning a read-only helper.

### Steps

1. Identify your current task from your state file:
   - Glob `~/.claude/plans/*/state*.md` for files matching your orch name
   - Read the active task section
2. Decide what aspect is most parallelizable:
   - Code exploration / understanding a module
   - Test analysis / identifying failure patterns
   - Documentation or dependency research
   - Finding similar patterns across the codebase
3. Spawn **1 sub-agent** via the Agent tool with this prompt template:

```
You are a READ-ONLY helper. You MUST NOT edit files, run git commands, or write to comms.

## Task
Help with: [specific aspect you identified]

## Context
- State file: [absolute path to your state file]
- Relevant files: [list absolute paths]
- Current task: [description from state file]

## Instructions
[Specific research/analysis instructions]

## Constraints
- READ-ONLY: use Read, Glob, Grep, Bash(read-only commands) only
- No file edits, no git, no writes to ~/.claude/comms/
- Return findings in ≤500 words
- Focus on actionable insights, not summaries
```

4. Continue working on your part while the sub-agent runs
5. When the sub-agent returns, integrate its findings

## Cross-Pleh (target agent specified)

Create a 3x force multiplier: target keeps working + your helper + helper's sub-agent.

### Steps

1. Parse the target agent name from $ARGUMENTS
2. Read the target's context (in parallel):
   - `~/.claude/comms/<target>/directives.md` — their current directive
   - Glob `~/.claude/plans/*/state*.md` for files matching the target name
   - `~/.claude/agent-memory/<target>/MEMORY.md` (if it exists — read separately, may not exist)
3. Spawn **1 helper sub-agent** via the Agent tool with this prompt template:

```
You are a helper for agent `<target-name>`. You are READ-ONLY but you CAN spawn your own sub-agent.

## Target's Directive
[paste relevant DIR from directives.md]

## Target's Current State
[paste active task section from state file]

## Target's Memory (if available)
[paste or summarize MEMORY.md content]

## Your Job
1. Analyze the target's current task and identify what research/exploration would help
2. Spawn your OWN sub-agent (via Agent tool) for parallel exploration — you are authorized to do so
3. Your sub-agent should tackle a different aspect than you (divide and conquer)
4. Combine your findings with your sub-agent's findings

## Sub-Agent Prompt Template
When you spawn your sub-agent, use this structure:

"You are a READ-ONLY helper. You MUST NOT edit files, run git commands, or write to comms.
Task: [specific research aspect different from what you're doing]
Files to examine: [absolute paths]
Return findings in ≤300 words. Focus on actionable insights."

## Constraints
- You are READ-ONLY: use Read, Glob, Grep, Bash(read-only commands) only
- No file edits, no git, no writes to ~/.claude/comms/
- Your sub-agent is also READ-ONLY
- Return combined findings in ≤500 words
- Focus on actionable insights the target can use immediately
```

4. When the helper returns (with its sub-agent's findings integrated):
   - If you are **meta**: summarize findings and tell the user the results
   - If you are **meta** and want to relay to the target: append an addendum to `~/.claude/comms/<target>/directives.md` (NOT a new DIR — just a note under the current DIR)
   - If you are **any other agent**: present findings to the user for relay

## Examples

- `/pleh` → self-help: spawn 1 helper for your current task (2x)
- `/pleh orch-example-project-p3` → cross-help: spawn helper + helper's sub for orch-example-project-p3's task (3x)
- `/pleh scaf` → cross-help: spawn helper chain for scaf's current work (3x)
