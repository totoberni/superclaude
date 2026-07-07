---
name: wf-report
description: "Use when driving a LaTeX report to a sealed finish via /converge: compile gate plus hostile-review SEAL. Meta+orch only."
category: workflow
user-invocable: true
argument-hint: "<report-target> [--rounds N] [--strict] [--publish]"
allowed-tools: Read, Write, Edit, Bash, Agent, Skill
---

# wf-report

Thin `/converge` binding that drives a LaTeX research report to a dual-gate seal: a deterministic compile check plus a fresh `w-hostile-reviewer` SEAL. This skill fixes the artifact class, producer protocol, and reviewer identity; `/converge` owns every mechanic (rounds, ledger, the 8 loop rules, goal-string emission). Read `/converge` first; this file states only the deltas.

**Authority**: meta + orch only, same as the engine it binds. Workers cannot spawn reviewers, so invoking this skill from a `w-*` is a no-op error.

## What this binds

Binding **B1** (goal-sealed convergence) by default: one report iterated to a clean seal. **B2** (phased campaign) suits a very large multi-chapter document (a thesis) where each chapter converges as its own phase; name it explicitly with `--binding B2` on the underlying `/converge` call, otherwise the default stays B1.

Artifact class: `methodology` per `/review-dispatch`'s resolution table. Producer: `w-doc` running the `report` protocol (`research/references/report.md`, Principles 1-16). Under `--publish` the producer switches to `report-publish` (Principle 9 suspended: full derivations, literature-depth amendment, mandatory pre-submission hostile-review). Reviewer every round: `w-hostile-reviewer`, scope `both` (methodology and technical gauntlets).

## Loop body (per round)

1. **PRODUCE**: dispatch `w-doc` to revise the report per `research/references/report.md` (round 1 builds/extends; later rounds work the prior round's punch list only).
2. **PERSIST**: the producer writes the artifact to disk; the conductor appends a ledger entry (round, delta, open-findings count) before any review runs.
3. **REVIEW**: a DUAL gate, verified independently of the producer's own claims (loop rule c, tool-verified critique); both legs must be clean.
   - (a) **Compile gate** (deterministic, no LLM, same class as `figure-validate`): the conductor runs `latexmk -pdf` (or the project's documented build command) directly.
     Pass requires clean exit and zero `Overfull \hbox` / undefined-reference / undefined-citation warnings that change meaning. Run this leg first: a dirty compile fails
     the round and skips the hostile-review dispatch outright, since there is nothing sound yet to review. Never accept the producer's own "it compiles" claim as evidence.
   - (b) **Hostile-review gate**: resolve via `/review-dispatch methodology <target> --scope both`, which spawns `w-hostile-reviewer` (opus, rubric
     `skills/hostile-review/SKILL.md`, isolation: artifact + diff + rubric only). Returns `VERDICT: REWORK|CLEAN blocking=N major=N minor=N round=K`.
4. **REPORT**: quote the reviewer's VERDICT line verbatim into the transcript and the ledger, beside the compile gate's pass/fail for the same revision.
5. **TRIAGE**: accepted findings become next round's punch list; contest the rest with evidence (file:line, or a named `report.md` principle and clause).

## Goal predicate

Convergence requires BOTH conditions, on the SAME artifact revision:

1. `SEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0` from a FRESH `w-hostile-reviewer` seal audit; `nits=0` belongs to the gate/strict bar, and `--strict` additionally requires two consecutive clean SEALs (`verdict-schema.md`, Bar levels).
2. A clean compile gate: `latexmk` exit 0, zero meaning-changing warnings.

A clean SEAL over a report that does not compile is not a seal; a clean compile with open hostile findings is not a seal either.

## Emitted /goal block

Setup ends by printing this block, then stops; wf-report never arms `/goal` itself (DEC-R2):

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH w-hostile-reviewer return (scope both), is the MOST RECENT such line, and post-dates the last change to the report, reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); (2) the producer has separately stated completion (STATUS: DONE); (3) the latest revision compiles clean (latexmk, zero material warnings). If review rounds exceed <N> (from --rounds, else 4), or total findings do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Paste this to arm the engine; wf-report does not self-arm.

## Constraints

- **NEVER** treat the compile gate as advisory: a dirty compile fails the round regardless of the hostile-review outcome, and blocks dispatching the reviewer at all.
- The compile gate is deterministic, the same class as `figure-validate`: a build tool's exit code and warning count, never an LLM's opinion that a document "looks like it compiles".
- **NEVER** let `--publish` silently change the artifact class: it switches the producer protocol to `report-publish`, and must show in the round's ledger entry.
- Inherits every `/converge` constraint unmodified (no self-arm, VERDICT and SEAL provenance, fresh seal auditor, reviewer isolation, 5-worker cap): this binding narrows artifact class and reviewer identity, nothing more.

## Cross-References

- Engine mechanics, the 8 loop rules, ledger, ratified DEC-R1/R2/R5: `~/.claude/skills/converge/SKILL.md`
- Reviewer resolution (`methodology` -> `w-hostile-reviewer`, opus, rubric preload, isolation): `~/.claude/skills/review-dispatch/SKILL.md`
- Rubric substance (gauntlets, severity, output structure): `~/.claude/skills/hostile-review/SKILL.md`
- Producer protocol (`report` / `report-publish` / `report-technical`, Principles 1-16): `~/.claude/skills/research/references/report.md`
- Token protocol, severity mapping, deterministic-checker row: `~/.claude/skills/_shared/verdict-schema.md`
