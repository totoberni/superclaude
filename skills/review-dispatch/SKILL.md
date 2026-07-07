---
name: review-dispatch
description: "Use when resolving the correct adversarial reviewer for an artifact by class and stakes: model, rubric, isolation, verdict contract."
category: delegation
user-invocable: true
argument-hint: "<artifact-class> <target> [--stakes normal|gate] [--panel]"
allowed-tools: Read, Bash, Agent, Skill
---

# Review Dispatch

The reviewer-dispatch resolver. Given an artifact class and stakes, it resolves and spawns the correct adversarial reviewer with the right model, effort, rubric, isolation, and verdict contract. It assembles a specified dispatch; it does not re-decide review policy.

**Authority**: meta + orch only. Workers (`w-*`) cannot spawn children; invoking this skill from a worker is a no-op error.

## Resolution table (artifact class -> reviewer, model, rubric, mode)

| `<artifact-class>` | Reviewer | Model | Rubric source (read by path) | Mode |
|---|---|---|---|---|
| `code-small` (<=3 files) | w-reviewer | sonnet | `skills/code-quality/SKILL.md` | adversarial |
| `code-large` / architectural | w-reviewer | opus | `skills/code-quality/SKILL.md` | adversarial |
| `design` (frontend) | w-design-reviewer | sonnet (opus cross-page) | `skills/design-review/SKILL.md` | adversarial |
| `methodology` / research / reports | hostile-review gauntlet | opus | `skills/research/SKILL.md` § hostile-review | adversarial |
| `infra` (`~/.claude` changes) | w-reviewer | sonnet / opus by scale | `skills/infra-security/SKILL.md` | adversarial |
| `figures` | figure-validate (no LLM) | n/a | `skills/figure-validate/SKILL.md` | deterministic |
| `test-integrity` | w-reviewer | sonnet | `skills/sanity-check/SKILL.md` (5-category) | adversarial |

Row notes:
- `code-large`: opus follows the numeric-budget model split; a large diff exceeds the sonnet call band.
- `design`: escalate to opus when the scope is cross-page consistency, not a single view.
- `methodology`: default `/research hostile-review --scope both` over the changed files. Until the standalone extraction lands (campaign W2.1), instruct the reviewer to read the hostile-review section of `skills/research/SKILL.md` by path. The max-effort execution mandate is satisfied STRUCTURALLY once `w-hostile-reviewer` (frontmatter `effort: max`) lands in W2.2; until then the dispatch goes to an opus reviewer and NOTES that max-effort depth is best-effort only. Do not request depth via a thinking keyword (retired on adaptive models, doctrine delta 1); depth is structural via frontmatter effort.
- `infra`: sonnet for a small single rule/hook edit; opus for multi-file, settings-surface, or security-surface changes.
- `figures`: deterministic gate, NO LLM reviewer. Run `/figure-validate`; the `/goal` predicates read the checker output directly. Severity maps via the verdict-schema deterministic-checker row (failed gate), not a VERDICT token.
- `test-integrity`: the sanity-check five-category rubric; the anti-hacking sweep is an automatic `blocking` finding (verdict-schema).

Rubric delivery: reviewers read the rubric by path today. Once Skill grants land fleet-wide (doctrine delta 4), the same rubric preloads via `skills:` frontmatter and the reviewer invokes it by slash; same rubric, different delivery.

## Dispatch assembly (every resolved dispatch)

Each dispatch carries the four-part contract (`_shared/dispatch-contract.md`) plus:

1. **Reviewer isolation**: send the artifact + diff + rubric ONLY. Never the producer's reasoning, self-assessment, or prior clean verdicts. Independence is load-bearing for honesty findings.
2. **R-2 baseline injection** (`/commit false` repos): stash the baseline (`git -C <repo> status --short` and `diff` to `/tmp/<session>-baseline.*`) and inject the baseline path with explicit "these files are PRIOR state, not your concern" guidance. Prevents dirty-tree false-positive REWORKs.
3. **Verdict contract, quoted verbatim** into the reviewer prompt: line 1 of the return MUST be `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K` (verdict-first survives truncation). Gate-stakes final acceptance uses `SEAL: ACCEPTED|REJECTED blocking=N major=N minor=N nits=N` from a FRESH holistic auditor, never the round reviewer.
4. **Numeric budget** (`_shared/dispatch-contract.md` §5): state a review band of 10-20 calls; expected above ~20-24 overrides `model: opus` regardless of worker class (sonnet truncates destructively past ~24; opus truncation is report-only). Hard architectural cap ~40 calls / ~250k tokens; hitting the ceiling means checkpoint + `STATUS: PARTIAL`, never a silent overrun.
5. **Named-skills-only boundary**: name the allowed rubric skill explicitly. Every other visible skill (CLI built-ins, plugin skills, owner-opt-in commands) is off-limits to the reviewer.
6. **maxTurns backstop**: note the reviewer agent's `maxTurns` as the structural ceiling behind the numeric budget, and name the checkpoint path so load-bearing findings hit disk before the final message.

Evidence bar (verdict-schema): findings without a `file:line` citation (or DOI/arxiv id, re-run expected-vs-actual, named principle + clause) are dropped before counting. A zero-finding review is valid.

## Panel assembly (`--panel`, or `--stakes gate`)

Reserved for irreversible gates (DEC-R5). Triggered by `--panel` or by `--stakes gate`.

- 2-3 judges on diverse tiers (mix sonnet and opus; AT LEAST one opus), each given a DIFFERENT rubric scope (for example: one code-quality, one infra-security, one test-integrity) so blind spots do not overlap.
- Majority vote decides. The conductor (you) consolidates and QUOTES each judge's `VERDICT`/`SEAL` line verbatim into the transcript; the conductor never authors a token.
- Final gate acceptance is a single `SEAL` from a fresh holistic auditor, not any round reviewer (verdict-schema provenance).

## Pre-dispatch announcement (print before Agent calls)

```
Artifact-class: <code-small|code-large|design|methodology|infra|figures|test-integrity>
Reviewer:       <w-reviewer|w-design-reviewer|hostile-review|figure-validate (deterministic)>
Model:          <sonnet|opus>   Budget: <N> calls   maxTurns backstop: <M>
Rubric:         <path>  (read-by-path | preloaded)
Isolation:      artifact + diff + rubric only;  baseline: <path or n/a>
Panel:          <n/a | 3 judges tiers=sonnet/opus/opus scopes=X/Y/Z, majority vote>
```

## Constraints

- NEVER dispatch a reviewer that saw the producer's reasoning or prior clean verdicts; isolation is the whole point.
- NEVER let a producer or the conductor author a `VERDICT`/`SEAL` line; only reviewer subagents emit them, quoted verbatim.
- NEVER spawn an LLM reviewer for a `figures` class; it takes the deterministic gate only.
- NEVER exceed the ~40-call / ~250k-token cap in one dispatch; split the review instead.
- NEVER add an unnamed skill to a reviewer's menu; name the rubric, everything else is off-limits.
- NEVER invoke this skill from a `w-*` worker; only meta and orch hold spawn authority.

## Cross-References

- Verdict tokens + severity map: `~/.claude/skills/_shared/verdict-schema.md`
- Four-part contract, numeric budget, isolation: `~/.claude/skills/_shared/dispatch-contract.md`
- Convergence loop that consumes these verdicts: `/converge`
- Quality gates (R-1/R-2/R-3/R-4): `~/.claude/rules/40-swarm-quality-gates.md`
- Worker model x effort matrix (thinking column stale per doctrine delta 1): `~/.claude/rules/13-worker-first-mandate.md` § Per-Worker Defaults
- Sister dispatch skill: `/swarm-dispatch`
