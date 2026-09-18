> Part of /research (see ../SKILL.md). Subcommand: gap-audit.

## `gap-audit` -- Conceptual-vs-technical divergence diagnostic

**Use when**: the design is approved (or the project has a mature
design document) and the implementation has landed or is nearly
landed, and you want to verify they correspond. Operationalises
Principle 12 as a standalone check. Catches the silent failure where
design says X, code does Y, both are internally consistent so
nothing errors.

**Args**: `gap-audit [--design <path>] [--implementation-scope <path-or-glob>] [--target <path>]`

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

### Remediation and confirmation (single pass)

The audit above is diagnostic only: it names the gaps but does not close them. Closing them is a
single remediate-then-confirm pass, run by the caller (meta, or a session that can spawn workers).
There is no mandated iteration protocol.

1. **Remediate**: a producer worker fixes the mappings flagged as gaps, Blocking first, then Major.
   Fixes land in the implementation or, where the design was the stale side, in the design
   document, whichever the gap's status prescribes. A gap the producer cannot close is contested
   with evidence (file:line, an expected-vs-actual re-run, or a named principle and clause), never
   silently downgraded. The producer returns `STATUS: DONE|PARTIAL|FAILED` as line 1 and never
   self-certifies the mapping.
2. **Persist**: the producer writes the updated mapping (regenerated gap table plus remediation
   list) to the same `gap-audit-<N>.md` artefact. Checkpoint-first: the mapping lives on disk, not
   in the final message.
3. **Gate (deterministic)**: the caller re-runs the audit (Workflow steps 3 to 7) over the current
   code, rebuilds the gap table, and recounts unresolved Blocking and Major gaps. Same class as
   `figure-validate` or a compile gate: a grep-and-count result, never an LLM opinion, and never
   accepted on the producer's say-so.
4. **Confirm**: dispatch `w-hostile-reviewer` once, scope `methodology`, over the complete current
   mapping (artefact + rubric only; never the producer's reasoning or a prior clean verdict). Line 1
   of its return is `VERDICT: REWORK|CLEAN blocking=N major=N minor=N`; the caller quotes it
   verbatim. The technical gauntlet is out of scope for a concept-to-code divergence audit.
5. **Triage and stop**: accepted findings and still-unresolved gaps are fixed or carried as open
   items in section 4 with a rebuttal; then stop. A further audit or review pass happens only if
   the owner or meta judges it necessary.

#### Constraints

- **NEVER** author a `VERDICT` token as caller or producer; the deterministic re-audit yields a
  gap-count (a gate result), and only the `w-hostile-reviewer` subagent emits the `VERDICT`,
  quoted verbatim.
- **NEVER** downgrade or silently drop a Blocking or Major gap to reach zero; a gap the producer
  cannot close is contested with evidence or carried as open.
- **NEVER** let the deterministic re-audit stand in for the review, or a `VERDICT: CLEAN` stand in
  for a zero gap-count; they are separate checks.

### Cross-References

- Token protocol, severity map, deterministic-checker row: `~/.claude/skills/_shared/verdict-schema.md`
- Reviewer rubric: `~/.claude/skills/hostile-review/SKILL.md`
- Research router: `../SKILL.md`
