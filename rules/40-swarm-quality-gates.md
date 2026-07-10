# Swarm Quality Gates

Critical rules that must be enforced when dispatching swarms or autocommissioning workers. These were promoted from `13-worker-first-mandate.md` § Critical Rules into a standalone rule for structural prominence — they are quality gates, not patterns.

## R-1: Schema Spec Pre-Commit (parallel workers with shared output)

When ≥2 parallel workers will produce/consume the same artefact (JSON, CSV, file paths, code structures), one of:
- (a) Pre-commit a schema spec doc both reference, OR
- (b) Sequence the workers (producer first), OR
- (c) Over-specify identical key paths in BOTH directives

Phase-4 example-mlmodel wave required post-hoc hoist due to schema mismatch — this rule prevents recurrence.

## R-2: Auto-Baseline-Stash for /commit false Repos

On session start when project policy = `/commit false`, auto-stash baseline:
```bash
git -C <repo> status --short > /tmp/<session_id>-baseline.txt
git -C <repo> diff > /tmp/<session_id>-baseline.diff
```
Inject baseline path into every `w-reviewer` dispatch prompt. Mitigates dirty-tree attribution false-positive REJECTs.

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

Track override patterns via `memory_db.py list --tier shared-global` (use /lt-mem to write). Promote → permanent only after the 3× threshold.

For one-offs: use `/autocommission` to spawn ephemeral worker (auto-cleanup on task done).

## R-5: No pre-approval (no inherited approval)

Standing rule for ALL convergence and review work (promoted from doctrine delta 7; PERSISTENT, not campaign-scoped).

Every VERDICT and every SEAL derives from a **fresh examination of the CURRENT state**, with evidence gathered THIS round. Approval never transfers across rounds:
- No approving round N+1 on the strength of round N.
- No anticipating a future clean state.
- No resting on "prior findings addressed" without re-examining the current artefact and citing fresh `file:line` evidence.

A SEAL binds to a **named artefact revision** (commit hash or round); any later change to the artefact voids it and requires a fresh SEAL. The conductor never quotes a stale token; the goal evaluator acts only on the MOST RECENT SEAL that post-dates the last change. The producer's completion is a signal, not an approval; the sole approval is an independent fresh auditor that examined the COMPLETE final state, not a delta.

SOT: `skills/_shared/verdict-schema.md` (§ No pre-approval + Canonical /goal block). Consumer: `/converge`.

R-5 is also enforced mechanically by the autonomous driver (`scripts/swarm/converge_auto.py`): fresh single-use sessions per phase, a producer token ban, commit-hash or content-manifest seal binding with post-seal re-verification, stale-seal rejection, and a post-commit seal-void hook.

## Enforcement

- R-2 baseline-stash: enforced by `~/.claude/hooks/modules/15-baseline-stash.sh` (when policy=/commit false)
- R-3 worker verification: enforced by orch.md § Worker Verification protocol
- R-1 schema spec: enforced by `/swarm-dispatch` skill checks
- R-4 fleet expansion: enforced by `/promote` skill (queries DB `shared-global` tier for ≥3-occurrence patterns)
- R-5 no pre-approval: enforced by `/converge` and `/review-dispatch` (fresh-auditor SEAL, most-recent and post-change); SOT `skills/_shared/verdict-schema.md`
- R-5 mechanical enforcement (unattended loops): `scripts/swarm/converge_auto.py` (fresh sessions, producer token ban, manifest/commit seal binding) and `scripts/swarm/seal-void-hook.sh` (post-commit seal-void check; when installed via `--install-void-hook`)

## Cross-References

- `13-worker-first-mandate.md` (matrix SOT — model × effort × thinking)
- `12-agent-hierarchy.md` (write scopes for spawn-capable agents)
- `skills/_shared/verdict-schema.md` (No pre-approval SOT for R-5)
- Skills: `/swarm-dispatch`, `/autocommission`, `/promote`, `/converge`, `/review-dispatch`, `/wf-auto`, `/swarm-observe`
