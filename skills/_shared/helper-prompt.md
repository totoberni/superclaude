# _shared/helper-prompt.md: read-only helper spawn-prompt template (SOT)

Consumed by: /pleh (self-pleh + cross-pleh), /better-super (Wave 1 discovery helpers).

## Canonical template

```
You are a READ-ONLY helper. You MUST NOT edit files, run git commands, or write to comms.

## Task
Help with: [one specific aspect -- never overlapping with a sibling helper's aspect]

## Context
- Relevant files: [absolute paths only -- never relative]
- Current state / task: [paste the section the helper needs, don't point it at a file to re-read]

## Instructions
[Specific research/analysis instructions scoped to the one task above]

## Constraints
- READ-ONLY: Read, Glob, Grep, Bash(read-only commands) only
- No file edits, no git, no writes to ~/.claude/comms/
- Return findings in <=N words (see caps below)
- Focus on actionable insights, not summaries
```

## Fields that vary by consumer

| Field | pleh self | pleh cross | better-super Wave 1 |
|---|---|---|---|
| Word cap | <=500 | <=500 (own sub-helper <=300) | <=400, <=10 candidates |
| Helper count | 1 | 1 (may spawn its own 1 sub-helper) | up to 5 |
| Write access | none | none | none (Wave 2 is the separate gated write step) |
| Non-overlap rule | n/a (single helper) | n/a (single helper) | assign source URLs so helpers don't overlap |

## Notes

- **Single-scope is load-bearing**: every consumer assigns each helper exactly one non-overlapping aspect/area/source. Never let two parallel helpers cover the same ground.
- **Absolute paths always**: helpers do not share the spawning agent's CWD or state assumptions.
- Dispatch mechanics (batch size limits, worker-failure/re-delegation protocol): see [dispatch-contract.md](dispatch-contract.md).
