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
