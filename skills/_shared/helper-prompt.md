# _shared/helper-prompt.md: read-only helper spawn-prompt template (SOT)

Consumed by: /pleh (self-pleh + cross-pleh), /delegate, /better-super (Wave 1 discovery helpers), /super-health (Step 4 post-hoc audit).

## Canonical template

```
You are a READ-ONLY helper. You MUST NOT edit files, run git commands, or write to comms.

## Task
Help with: [one specific aspect -- never overlapping with a sibling helper's aspect]

## Context
- Relevant files: [absolute paths only -- never relative]
- Current state / directive: [paste the section the helper needs, don't point it at a file to re-read]

## Instructions
[Specific research/analysis instructions scoped to the one task above]

## Constraints
- READ-ONLY: Read, Glob, Grep, Bash(read-only commands) only
- No file edits, no git, no writes to ~/.claude/comms/
- Return findings in <=N words (see caps below)
- Focus on actionable insights, not summaries
```

## Fields that vary by consumer

| Field | pleh self | pleh cross | delegate | better-super Wave 1 | super-health Step 4 |
|---|---|---|---|---|---|
| Word cap | <=500 | <=500 (own sub-helper <=300) | n/a -- workers write code | <=400, <=10 candidates | <=600 |
| Helper count | 1 | 1 (may spawn its own 1 sub-helper) | up to 5 | up to 5 | exactly 5, one per fixed area |
| Write access | none | none | scoped write (edits within assigned file scope) | none (Wave 2 is the separate gated write step) | none |
| Non-overlap rule | n/a (single helper) | n/a (single helper) | non-overlapping files | assign source URLs so helpers don't overlap | one fixed audit area each |

## Notes

- **/delegate is the one non-read-only consumer**: its `w-*` workers may edit within an explicitly stated file scope. The shared STRUCTURE (absolute paths, single-scope statement, explicit constraints) still applies; the "READ-ONLY" line is replaced with an explicit write-scope statement plus a success-criteria line.
- **Single-scope is load-bearing**: every consumer assigns each helper exactly one non-overlapping aspect/area/source. Never let two parallel helpers cover the same ground.
- **Absolute paths always**: helpers do not share the spawning agent's CWD or state assumptions.
- Dispatch mechanics (batch size limits, worker-failure/re-delegation protocol): see [dispatch-contract.md](dispatch-contract.md).
