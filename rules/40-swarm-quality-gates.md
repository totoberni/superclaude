# Swarm Quality Gates

Critical rules that must be enforced when dispatching swarms or autocommissioning workers. These were promoted from `13-worker-first-mandate.md` § Critical Rules into a standalone rule for structural prominence — they are quality gates, not patterns.

## R-1: Schema Spec Pre-Commit (parallel workers with shared output)

When ≥2 parallel workers will produce/consume the same artefact (JSON, CSV, file paths, code structures), one of:
- (a) Pre-commit a schema spec doc both reference, OR
- (b) Sequence the workers (producer first), OR
- (c) Over-specify identical key paths in BOTH dispatch prompts

Phase-4 example-mlmodel wave required post-hoc hoist due to schema mismatch — this rule prevents recurrence.

## R-2: Auto-Baseline-Stash for No-Commit-Projects Convention

On session start when the repo matches the no-commit-projects convention (repos where agents do not auto-commit; owner commits manually; detected via `CLAUDE_COMMIT_POLICY` or `hooks/no-commit-projects.local`), auto-stash baseline: the baseline-stash hook saves the output of `git -C <repo> status --short` to `~/.claude/tmp/<session_id>-baseline.txt` and of `git -C <repo> diff` to `~/.claude/tmp/<session_id>-baseline.diff`.
Inject baseline path into every `w-reviewer` dispatch prompt. Mitigates dirty-tree attribution false-positive REJECTs. This drives the baseline-stash reviewer-attribution hook only; it does not mechanically block commits (that is `/git`'s job, global).

## R-3: Worker Verification After Spawn

After every worker returns:
1. Read modified files (verify scope + correctness)
2. Run tests if applicable
3. `git diff --stat` (confirm only expected files changed)
4. Watch for: weakened assertions, added skips, scope violations
5. If wrong: fix yourself OR re-delegate with clearer instructions (NEVER redo their work — escalate or re-delegate)
6. Findings and claims require `file:line` evidence; never trust worker self-reports. Verify each claim against the cited lines before acting on it.
7. On worker **FAILURE**, resume or re-dispatch rather than redo the work yourself (v2.1.198 failure messages carry the worker's last output, so the run is resumable once the blocker clears). Checkpoint-first: workers write load-bearing findings to disk BEFORE their final message, so a truncated or failed return still leaves the evidence recoverable.

## R-4: Stringent w-* Fleet Expansion (DEC-001)

A new permanent `~/.claude/agents/w-*.md` is created IFF:
- Use case is **fundamentally missing** from the existing fleet (not a variation/scope adaptation), AND
- A `/autocommission` override for the same pattern has occurred ≥3 times across sessions

Track override patterns via `mem list --tier shared-global` (use /lt-mem to write). Promote → permanent only after the 3× threshold.

For one-offs: use `/autocommission` to spawn ephemeral worker (auto-cleanup on task done).

## Enforcement

- R-2 baseline-stash: enforced by the baseline-stash hook (when the repo matches the no-commit-projects convention)
- R-3 worker verification: enforced by the supervising agent following the steps above and prompted by the worker-verify reminder
- R-1 schema spec: enforced by `/swarm-dispatch` skill checks
- R-4 fleet expansion: enforced by `/promote` skill (queries DB `shared-global` tier for ≥3-occurrence patterns)

## Cross-References

- `13-worker-first-mandate.md` (matrix SOT — model × effort × thinking)
- `12-agent-hierarchy.md` (write scopes for spawn-capable agents)
- Skills: `/swarm-dispatch`, `/autocommission`, `/promote`
