# CCPM Task-Graph Metadata Format

## Purpose

Enable Meta to auto-decide swarm-dispatch batches by reading task metadata directly from `~/.claude/plans/*/plan.md`. Each task declares its parallelism, dependencies, and conflicts inline, so Meta can compute waves topologically without per-plan judgment calls.

Adapted from CCPM (Claude Code Project Management) — github.com/automazeio/ccpm.

## Convention

Tasks in plan.md phases declare metadata in trailing brackets after the task description:

```
- T2.1: Write w-implementer.md  [parallel: true, conflicts_with: T2.6]
```

Three keys are recognized:

| Key | Meaning |
|-----|---------|
| `parallel: true` | Safe to run alongside other `parallel: true` tasks in the same phase (subject to conflicts) |
| `depends_on: T*` | Must complete before this task starts. Comma-separated for multiple. |
| `conflicts_with: T*` | Mutually exclusive with this task — cannot share a swarm batch. Comma-separated for multiple. |

## Parser Rules

- Tasks without metadata default to `parallel: false, depends_on: <previous task in phase>` (sequential, classical plan.md behavior).
- `depends_on` references can be intra-phase (`T2.1`) or cross-phase (`T1.3`); Meta resolves the full DAG topologically.
- `conflicts_with` is symmetric — declaring it on one task implies it on the other. No need to repeat the declaration on both sides.
- Bracket key order does not matter — `[parallel: true, depends_on: T1.1]` and `[depends_on: T1.1, parallel: true]` are equivalent.
- Whitespace inside brackets is ignored.
- `parallel: true` overrides the implicit sequential default — the explicit metadata always wins.
- A task with explicit `depends_on` does NOT inherit the implicit "previous task in phase" dependency; explicit `depends_on` fully replaces the default.
- Unknown keys inside the bracket are ignored with a warning logged to Meta's run summary (forward-compatibility).

## Worked Example

```markdown
### Phase 2 — Worker Fleet Expansion
- T2.1: Write w-implementer.md           [parallel: true, conflicts_with: T2.6]
- T2.2: Write w-doc.md                   [parallel: true, conflicts_with: T2.6]
- T2.3: Write w-explorer.md              [parallel: true, conflicts_with: T2.6]
- T2.4: Write w-tester.md                [parallel: true, conflicts_with: T2.6]
- T2.5: Write w-committer.md             [parallel: true, conflicts_with: T2.6]
- T2.6: Update existing w-* frontmatter  [depends_on: T2.1, T2.2, T2.3, T2.4, T2.5]
```

Meta computes the following dispatch waves:

| Wave | Tasks | Mode |
|------|-------|------|
| 1 | T2.1, T2.2, T2.3, T2.4, T2.5 | Parallel batch of 5 (W-1/W-4 pattern via `/swarm-dispatch`) |
| 2 | T2.6 | Sequential — blocked on Wave 1 completion |

T2.6 cannot join Wave 1 because it conflicts with every other task; it cannot start until Wave 1 completes because it depends on all five.

## Edge Cases

- **More than 5 parallel tasks in a wave**: Meta splits into multiple consecutive batches of up to 5 (the swarm-dispatch parallel ceiling). Batches are emitted in task-ID order; subsequent waves wait only on tasks whose IDs they explicitly depend on, not on prior batches in the same wave.
- **Cross-phase `depends_on`**: Meta sequences phases — Phase N+1 cannot start any wave whose tasks depend on unresolved Phase N tasks. Phases otherwise execute eagerly as soon as their predecessors clear.
- **Circular `depends_on`**: Meta detects the cycle during DAG construction and escalates via ESC-NNN. No execution starts.
- **`parallel: true` with implicit `depends_on previous`**: explicit metadata wins. Task is parallel; the implicit-previous default is dropped.
- **Conflicts without parallel**: `conflicts_with` is only meaningful for parallel tasks. On sequential tasks it is silently ignored (sequential tasks never share a batch).
- **`depends_on` referencing an unknown task ID**: Meta escalates via ESC-NNN before any execution starts. Typos do not silently degrade to sequential.

## Migration Path

- Existing plans are NOT required to migrate. They continue to execute under classical sequential semantics.
- New plans should adopt the convention from creation.
- Migrate an existing plan when next editing it for unrelated reasons — opportunistic, not forced.

## Cross-References

- SOT rule: `~/.claude/rules/13-worker-first-mandate.md` § CCPM Task-Graph Metadata
- Source: github.com/automazeio/ccpm (CCPM)
- First adopter plan: `~/.claude/plans/swarm-first-v2/plan.md`
- Consumer skill: `/swarm-dispatch` — reads this metadata when dispatching parallel batches
