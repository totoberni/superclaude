# _shared/dispatch-contract.md: worker dispatch contract v2 (SOT)

Consumed by: /swarm-dispatch, /converge, /review-dispatch, /autocommission, meta/orch dispatch prompts. Extends rules/40 gates; verdict tokens per [_shared/verdict-schema.md].

Every Agent-tool spawn prompt from meta/orch carries the four parts (Anthropic multi-agent doctrine) plus three superclaude extensions.

## Four parts

1. **Objective**: one unambiguous goal, stated first.
2. **Output format**: expected artifact(s) + the verdict-schema first line (STATUS for producers, VERDICT for reviewers).
3. **Tool/source guidance**: exact files/paths/commands to consult, and what NOT to consult.
4. **Boundaries**: file scope (write allowlist), off-limits paths, read-only vs write, word caps.

## Extensions

5. **Numeric budget + model split** (data-grounded 2026-07-07): state a tool-call ceiling per class: recon 3-10; standard build/extract/review 10-20; complex multi-file 20-40. The ceiling also selects the model: expected <=20 calls, sonnet-class defaults are safe; expected >20-24 calls, override `model: opus` at dispatch regardless of worker class. Rationale: sonnet truncates predictably past ~24 calls with DESTRUCTIVE damage (red trees, half-applied edits); opus truncation is report-only (work complete and correct underneath; checkpoint-first + verdict-first recover it). An architectural cap of ~40 calls / ~250k tokens stands for ALL models: never scope a single dispatch beyond it, split the task instead. Worker treats the ceiling as a hard boundary; hitting it means checkpoint + STATUS: PARTIAL, not silent overrun. (`maxTurns` in the agent def is the structural backstop. Evidence: memory `worker-model-sonnet-truncation-opus-experiment`.)
6. **Checkpoint-first**: the dispatch names a checkpoint path (default `~/.claude/plans/<campaign>/checkpoints/<task-id>.md`). The worker writes findings/progress there BEFORE composing its final message; the final message may be a pointer + summary. The parent never depends on the final message for load-bearing content. If the worker approaches its output budget before the task is finished, it must STOP and report exactly what is complete, what remains, and any uncommitted or partial state; it must never fabricate completion, silently drop work, or weaken/skip the task to fit. Producers close with `STATUS: PARTIAL`; reviewer/seal roles close with a partial verdict noting the incomplete scope instead. Either way, the checkpoint plus a clean partial report let the parent finish or re-dispatch (see `/recover-truncated`).
7. **Reviewer isolation**: dispatches to reviewers include artifact + diff + rubric ONLY. Never the producer's reasoning, self-assessment, or prior clean verdicts. Every reviewer dispatch prompt MUST carry a `Ledger: <path>` line (bare or bullet-prefixed) naming the round ledger; the 62-review-dispatch guard blocks any reviewer spawn that omits it or names a non-existent path.

## Constants

- Swarm ledger keys on **agentId**, never name (respawned names are refused sends). Explore/Plan are one-shot: anything steerable uses a custom w- type.
- On worker failure: steer/resume before respawn; the failure end-message carries last output (R-3 extension: resume-never-redo).
- 5 workers per batch, hard cap. Uncertain calls never batched with safe ones.
- Trigger escaping per rules/13 § Trigger Escaping: the three owner tokens stay dot-escaped in any authored prompt.
- R-1: if 2+ workers share an output artifact, pre-commit the schema in the dispatch or sequence the workers.
- Skill-granted workers: the dispatch names the allowed skills explicitly; every other visible skill is off-limits (menus include CLI built-ins, plugin skills, and owner-opt-in commands that workers must never fire).
