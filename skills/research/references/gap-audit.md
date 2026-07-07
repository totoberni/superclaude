> Part of /research (see ../SKILL.md). Subcommand: gap-audit.
> With the convergence loop below (a thin `/converge` binding), `/research gap-audit` gains a
> DRIVER role: it does not just report the concept-to-code gaps once, it iterates fixes to zero.
> The single-pass diagnostic (Workflow, Output structure) is the round-1 audit; the loop drives it
> to a sealed finish.

## `gap-audit` -- Conceptual-vs-technical divergence diagnostic

**Role**: a `/converge` DRIVER over conceptual-vs-technical divergence. One pass produces the gap
table; the loop remediates the flagged mappings and re-audits until zero unresolved Blocking or
Major concept-to-code gaps remain and a fresh methodology auditor seals the result.

**Use when**: the design is approved (or the project has a mature
design document) and the implementation has landed or is nearly
landed, and you want to verify they correspond. Operationalises
Principle 12 as a standalone check. Catches the silent failure where
design says X, code does Y, both are internally consistent so
nothing errors.

**Args**: `gap-audit [--design <path>] [--implementation-scope <path-or-glob>] [--target <path>] [--rounds N] [--strict]`

### Workflow

1. Run the project discovery protocol.
2. Identify the design document: the pre-registered plan, decision
   log, extend-notes, or experiment-design document. Fall back to
   `--design <path>` if ambiguous.
3. Enumerate the conceptual decisions: what variables, what
   controls, what metrics, what thresholds, what data, what
   pre-registered predictions, what triangulation pairs.
4. For each, grep the implementation for the concrete realisation.
   `--implementation-scope` narrows the grep target; default is the
   project root.
5. Build the gap table:
   `| # | concept | design says | impl does | evidence | status |`
   where `status` is one of: EQUAL, EQUIVALENT (semantically same,
   syntactically different -- document why), DIVERGENT-HARMLESS,
   DIVERGENT-HARMFUL, NOT-IMPLEMENTED, NOT-IN-DESIGN (impl has a
   detail the design did not cover; propose whether design should
   add it or impl should remove it).
6. Severity-rank gaps: DIVERGENT-HARMFUL and NOT-IMPLEMENTED default
   to Blocking; EQUIVALENT-with-missing-rationale to Major; clean
   equivalents to PASS. Same severity conventions as
   `hostile-review`.
7. Special case: dataset-realisation shifts (Principle 13). Run
   re-verification as part of the audit; flag every cited number not
   re-checked against latest source of truth as DIVERGENT-HARMFUL.
8. Output a Markdown artefact per `report-technical` conventions.
   Default target: `<project-root>/docs/gap-audit-<N>.md` (auto-
   incremented) or `~/.claude/plans/<project>/gap-audit-<N>.md`.
9. The output feeds directly into `hostile-review`'s
   remediation-sequencing block; cite it in the final review.

### Output structure

```
# Conceptual-technical gap audit: <project> (audit <N>)

## 0. Scope
<design document path | implementation scope | date>

## 1. Concept catalogue
<table: concept | design says | design location | design rationale>

## 2. Gap findings
<table: # | concept | design | impl | evidence | status | severity>

## 3. Dataset-realisation audit
<numbers in prose | source of truth cell/script | re-verified? |
 mismatch magnitude>

## 4. Severity-ranked remediation
<ordered list: Blocking | Major | Minor; each finding's fix in one
 line>
```

### Convergence loop (converge DRIVER, binding B1)

The single-pass audit above is diagnostic only: it names the gaps but does not close them. As a
`/converge` DRIVER, gap-audit iterates fix-then-re-audit rounds until the severity-ranked
remediation list (section 4) is empty of Blocking and Major entries and a fresh auditor seals the
mapping. It is a thin binding of `/converge` on binding **B1** (goal-sealed convergence);
`/converge` owns every mechanic (round order, ledger, the 8 loop rules, caps, post-compaction
requote). This section states only the gap-audit-specific slots. Loop orchestration
(dispatching producers, invoking `/review-dispatch`, printing the `/goal` block, spawning the
fresh seal auditor) runs in the conductor's context (meta/orch, which holds Agent and Skill);
/research inherits its tools from the conductor, so this subcommand declares no allowed-tools
of its own.

- **Artifact**: the concept-to-code mapping, that is the gap table (Output structure section 2) plus
  its severity-ranked remediation (section 4), carried across rounds in the same `gap-audit-<N>.md`
  artefact.
- **Producer (per round)**: a producer worker that fixes the mappings flagged as gaps (round 1: the
  initial audit plus the first remediation pass; later rounds: the prior round's punch list only). A
  gap the producer cannot close is contested with evidence, never silently downgraded.
- **Gate (deterministic)**: the conductor re-runs the audit (Workflow steps 3 to 7) over the current
  code, rebuilds the gap table, and recounts unresolved Blocking and Major gaps. Same class as
  `figure-validate` or a compile gate: a grep-and-count result, never an LLM opinion.
- **Seal auditor**: a FRESH `w-hostile-reviewer`, scope `methodology`, resolved via
  `/review-dispatch`; it examines the complete final mapping on the sealing round and emits the
  terminating `SEAL`.

#### Loop body (per round)

Converge's five steps, filled with gap-audit content:

1. **PRODUCE / REVISE**: the producer fixes the concept-to-code mappings flagged as gaps (round 1:
   run the audit end to end, then remediate the top Blocking and Major gaps; later rounds: work the
   prior round's punch list only). Fixes land in the implementation or, where the design was the
   stale side, in the design document, whichever the gap's status prescribes.
2. **PERSIST**: the producer writes the updated mapping (regenerated gap table plus remediation
   list) to the `gap-audit-<N>.md` artefact; the conductor appends a ledger entry (round, delta,
   open-gap count) before any review runs. Checkpoint-first: the mapping lives on disk, not in the
   final message.
3. **REVIEW**: re-run the gap-audit. The conductor re-audits deterministically (Workflow steps 3 to
   7) and recounts unresolved Blocking and Major gaps; this gap-count is the per-round signal. On
   the sealing round a FRESH `w-hostile-reviewer` methodology pass confirms the mapping over the
   complete final state and emits the `SEAL` (verdict-schema: only a fresh reviewer subagent may
   author the terminating token; the conductor's deterministic re-audit is a gate result, not a
   token).
4. **REPORT**: the conductor records the re-audit's unresolved-gap count in the ledger each round,
   and quotes the reviewer's `SEAL` line verbatim at seal time. Producers never author a `VERDICT`
   or `SEAL`.
5. **TRIAGE**: unresolved gaps become the next round's punch list; a gap the producer contests is
   logged with a rebuttal (file:line, an expected-vs-actual re-run, or a named principle and
   clause), not carried as silently closed.

#### Goal predicate

Convergence requires ALL, on the SAME artefact revision: (1) a FRESH `w-hostile-reviewer` (scope
`methodology`) returns a clean `SEAL: ACCEPTED blocking=0 major=0 minor=0` (`nits=0` at the gate or
strict bar; `--strict` additionally requires two consecutive clean SEALs, see `verdict-schema.md`
Bar levels), quoted verbatim by the conductor; (2) the producer's own completion statement
(`STATUS: DONE`) in the same round; (3) the latest deterministic re-audit shows 0 unresolved
Blocking and Major concept-to-code gaps. A clean SEAL over a mapping that still carries open
Blocking gaps is not a seal; a zero-gap count without a fresh methodology SEAL is not a seal either.

#### Emitted /goal block

Setup ends by printing a ready-to-paste `/goal` block in the canonical shape (`verdict-schema.md`,
Canonical emitted /goal block), then STOPS. The external judge stays independent; gap-audit never
arms `/goal` itself (DEC-R2). `/goal` takes a natural-language CONDITION; never invent a
`/goal seal ...` subcommand.

```
/goal Accept only when ALL hold: (1) the transcript contains a line beginning "SEAL: ACCEPTED" that the conductor states is quoted verbatim from a FRESH w-hostile-reviewer return (scope methodology), is the MOST RECENT such line, and post-dates the last change to the concept-to-code mapping (delta 7: a stale SEAL predating the latest fix never fires the goal), reporting blocking=0 major=0 minor=0 (nits=0 at gate/strict); (2) the producer has separately stated completion (STATUS: DONE); (3) the latest deterministic re-audit shows 0 unresolved Blocking or Major concept-to-code gaps. If review rounds exceed <N> (from --rounds, else 4), or total unresolved gaps do not decrease across 2 consecutive rounds, declare ESCALATE and stop.
```

Print the block, then stop. The human pastes `/goal` to arm the engine.

#### Constraints

- **NEVER** author a `VERDICT` or `SEAL` token as conductor or producer; the deterministic re-audit
  yields a gap-count (a gate result), and only a fresh `w-hostile-reviewer` subagent emits the
  terminating `SEAL`, quoted verbatim.
- **NEVER** downgrade or silently drop a Blocking or Major gap to reach zero; a gap the producer
  cannot close is contested with evidence or carried as open into the next punch list.
- **NEVER** let the round's deterministic re-audit stand in for the seal; the seal is always a FRESH
  `w-hostile-reviewer` methodology pass over the complete final mapping.
- **NEVER** widen the seal reviewer's scope beyond `methodology`; the technical gauntlet is out of
  scope for a concept-to-code divergence audit.
- **NEVER** arm `/goal` or `/loop` yourself; print the block and stop (DEC-R2).
- **NEVER** drive the convergence loop from a `w-*` worker; the loop spawns the seal reviewer, so
  only meta and orch hold spawn authority (the single-pass audit above carries no such restriction).
  Inherits every `/converge` constraint unmodified.

### Cross-References

- Loop engine, the 8 loop rules, ledger, caps, DEC-R2: `~/.claude/skills/converge/SKILL.md` (binding B1)
- Reviewer resolution (`methodology` -> `w-hostile-reviewer`): `~/.claude/skills/review-dispatch/SKILL.md`
- Token protocol, severity map, deterministic-checker row, canonical /goal block: `~/.claude/skills/_shared/verdict-schema.md`
- Sibling methodology-class driver (same B1 shape): `~/.claude/skills/wf-design/SKILL.md`
- Per-round and seal auditor rubric: `~/.claude/skills/hostile-review/SKILL.md`
- Research router: `../SKILL.md`
