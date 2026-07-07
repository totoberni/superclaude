---
name: hostile-review
description: "Use when a draft needs an adversarial truth-seeking stress test before a commit horizon: methodology and technical gauntlets."
category: research
user-invocable: true
argument-hint: "[--scope methodology|technical|both] [--target <path>] [--reviewer-count 1|2] [--web-search-budget <N>] [--markscheme <path>] [--spec <path>] [--mode A|B]"
allowed-tools: Read, Bash, Grep, Glob
---

> Formerly the `/research hostile-review` subcommand; `/research` still delegates here. The numbered Principles cited below (Principle 1-15, including Principle 3a) and "Cross-subcommand conventions" refer to the shared /research skill; see /research Principles (`../research/references/principles.md`) and /research Cross-subcommand conventions (`../research/SKILL.md`) rather than duplicating them here.

## `hostile-review` -- Adversarial second-reviewer pass

**Use when**: a first draft exists and you need a truth-seeking stress
test before the next commit horizon -- coursework submission, HPC
wave, paper submission, phase gate. The subcommand is deliberately
adversarial: it catches what self-review missed by assuming the
weakest interpretation of every claim and demanding evidence rather
than plausibility.

**Args**: `hostile-review [--scope methodology|technical|both] [--target <path>] [--reviewer-count 1|2] [--web-search-budget <N>] [--markscheme <path>] [--spec <path>] [--mode A|B]`

**Execution mandate**: run at **maximum reasoning depth**. When dispatched
as the `w-hostile-reviewer` agent this is guaranteed structurally by its
`effort: max` frontmatter (doctrine delta 1: prompt thinking keywords are
retired on adaptive-thinking models; depth is set via `effort:`, never a
keyword). If invoked in a context where maximum effort cannot be assured,
escalate rather than proceed shallow.

`--scope` selects the review lens. `methodology` audits scientific
design (hypotheses, variable isolation, falsifiability, triangulation,
novelty, decision rationale) and mirrors the hrev-1 pattern used on
example-project M2. `technical` audits implementation readiness (spec
completeness, codebase forensics, HPC portability, dependency graph,
single source of truth) and mirrors the hrev-2 pattern. `both` (the
default) runs both gauntlets in a single unified review; this is the
right choice for coursework projects where one orch owns the whole
stack.

`--reviewer-count` controls delegation. `1` (default) produces a
single review document. `2` is meaningful only with `--scope both` and
signals meta to dispatch two orchs -- one per scope -- with
independent review docs that meta later consolidates. Two-reviewer
dispatches are the right pattern when the methodology and technical
surfaces are large enough that a single session cannot cover both
within budget.

`--target` overrides the default output path (see below).
`--markscheme`, `--spec`, and `--mode` are escape hatches when the
discovery protocol cannot infer them.

### Hostile-specific principles (in addition to the 15 skill Principles)

The 15 skill Principles already supply most of the hostile-review
substrate (specificity, falsifiability, triangulation, threshold
defensibility as Principle 3a, literature-as-confounder, verdict-
language calibration). The items below are the *hostile posture* that
the principles alone do not encode.

1. **No charity**: assume the weakest interpretation of every claim.
   If a statement CAN be read as wrong, investigate whether it IS.
   The author has had their chance to self-review; you are the
   second reviewer and they will not read your notes during the
   review.

2. **Cite evidence, not opinion**: "I think this is wrong" is never
   a finding. Every finding carries
   `severity | location | issue | evidence | recommended fix`
   where evidence is one of: a file:line reference, a literature
   citation with arxiv ID or DOI, a numerical re-run with the
   expected vs actual values, a principle name with the clause it
   violates. A finding without evidence is dropped, not downgraded.
   (This restates Cross-subcommand conventions in the hostile
   context: the bar is identical but the posture is stricter.)

3. **Severity calibrated to blast radius**: Blocking (invalidates a
   conclusion, prevents correct execution, fails a spec requirement),
   Major (weakens a claim enough that a reviewer would push back),
   Minor (polish that improves but does not change the verdict),
   PASS (survives hostile scrutiny intact -- note these explicitly;
   a review that only reports problems is noise). Default to
   Blocking when a choice between Blocking and Major is genuinely
   ambiguous.

4. **Specificity over volume**: 10 specific findings with
   reproducible evidence outweigh 50 generic observations. If you
   catch yourself writing "the methodology is weak" without naming
   the variable/threshold/test, delete the sentence and keep
   searching for the specific failure.

5. **Proportionality to stage**: a pre-experiment hostile review
   weights hypothesis-and-protocol design; a mid-experiment review
   weights implementation and HPC readiness; a post-experiment
   review weights numerical faithfulness and spec coverage. Do not
   apply the implementation gauntlet to a design not yet written,
   or the novelty gauntlet to a coursework where novelty is not
   assessed.

6. **Multi-pass cadence**: for consequential work, budget 2-3
   hostile passes, not one. Pass 1 catches ~70 percent of big
   issues; pass 2 finds subtler tier-1 problems that only surface
   once pass-1 fixes are in (calibration hidden by the gross
   failure, colorbar inconsistency masked by the missing legend,
   numerical shift cascaded through updated prose); pass 3
   confirms the fixes held without introducing new issues. The
   cadence is not "a single review done harder"; it is a sequence
   of reviews with scope-explicit refinement between them.

### Mode-aware gauntlet selection

| Mode | Primary weights | Secondary |
|------|-----------------|-----------|
| A (publishing) | methodology + novelty validation + literature-as-confounder | technical when code/HPC exists |
| B (assignment) | markscheme alignment + specsheet fidelity + coverage audit | methodology + technical; extension-novelty audit when extensions exist (validate they strengthen rather than distract from core) |

### Methodology scope -- gauntlet

Apply when `--scope methodology` or `--scope both`. Covers scientific
design, not implementation.

1. **Per-experiment audit**: for every experiment, hypothesis, or
   research question, score each row PASS / WEAK / FAIL.

   | Check | Principle | Notes |
   |-------|-----------|-------|
   | Specificity | 1 | exact model/layer/split/eval mode |
   | Variable isolation | 2 | one IV, controls explicit |
   | Falsifiability | 3 | hypothesis + named statistical test |
   | Threshold defensibility | 3a | provenance for every numeric cutoff |
   | Statistical appropriateness | -- | named test matches data structure |
   | Sample size / error bars | -- | power analysis or empirical justification |
   | Confounder completeness | 5 | propose at least one unlisted |
   | Triangulation sufficiency | 6 | ≥2 independent operational definitions |
   | Ontological purpose stated | 4 | what truth this gives us |
   | Non-claim list explicit | -- | what the experiment does NOT support |
   | Pre-registration present | 11 | DEC-NNN before run |
   | Verdict language calibrated | 14 | strong verdicts only for multi-seed triangulated claims |

2. **Hypothesis-correctness check**: derive the unconditional
   distribution of the primary observable under H0. Flag any
   hypothesis whose prose prediction contradicts basic algebra
   (Principle 12 special case).

3. **Protocol audit**: for each measurement or analysis protocol,
   verify operational specificity (file, function, inputs, outputs,
   compute budget). Flag any "similar to X" that lacks a concrete
   spec.

4. **Triangulation matrix audit**: if the product names
   triangulation for any claim, verify independence of operational
   definitions. A matrix that lists "metric A batch 1" + "metric A
   batch 2" is not triangulation; name that.

5. **Decision log audit**: for each decision, is the rationale
   explicit? Does any decision contradict a prior decision or a
   paper convention? Are scope-shedding triggers and contingency
   thresholds pre-registered (Principle 11)?

6. **Novelty claim validation** (mandatory web search when the
   product claims novelty): targeted literature searches per
   claim; verdict NOVEL / PARTIALLY-SCOOPED / SCOOPED with evidence
   URLs. Novelty claims that narrow to a example-project-specific or
   project-specific reframing (e.g., "First mechanistic
   interpretability study of X, specifically on cross-repeat KV
   plus weight-tied mid-block") are valid when the reframing is
   explicit, not implicit.

7. **Metric-definition hygiene**: for optimisation-theoretic
   quantities (regret, pseudo-regret, inference loss), BO
   acquisitions (EI, max-variance, max-MI), uncertainty
   intervals (predictive CI vs latent CI, prior vs posterior,
   noisy observation vs clean function value), verify every
   metric has a definition table with name, symbol, sign
   convention, limiting cases, and ground-truth-vs-noisy
   distinction. Metrics defined after results are a principle
   violation (Principle 11) even when arithmetically correct.

8. **Bootstrap / drop-one sensitivity**: for any fitted
   exponent, slope, or threshold cited as evidence, verify the
   product reports bootstrap CI or drop-one sensitivity. If
   dropping a single outlier flips the narrative's direction,
   the data does not support the claim.

9. **Cross-cutting methodology sweeps**:
   - Reliability discipline (P6): triangulation operational
     independence — flag any pair sharing a forward pass and a
     derived statistic.
   - Ontological drift (P4): flag any RQ whose "what truth" reads
     as engineering motivation in disguise.
   - Scope creep (P7): is the budget realistic for the stated
     ceiling?
   - Multiple-comparisons hygiene (P10/P11): pre-registered
     primaries under family-wise control, secondaries under FDR;
     validation gates explicitly exempt from the family.
   - Monotonicity claims (P9): dense sampling near the turning
     point; reject claimed crossings inside a single CI width.
   - Reproduction fidelity (P8): all relevant paper configs
     matched; consider twin-pilot (reproduction-faithful +
     fair-comparison) where divergence would bias results.

### Technical scope -- gauntlet

Apply when `--scope technical` or `--scope both`. Covers
implementation readiness.

1. **Per-code-change audit**: for each code change, flag, or
   configuration item introduced in the product:
   - Is the spec complete enough for a code-wave orch to
     implement without supplementary questions?
   - Edge cases enumerated?
   - Downstream interactions traced (does the flag thread
     through all call sites)?
   - Default-preservation verified (does the default reproduce
     prior behaviour exactly)?

2. **Per-protocol implementation audit**: for each implementation
   protocol or module:
   - Operational spec (file path, function signature, input/output
     types, compute budget)?
   - Does the protocol match the research questions it claims to
     serve?
   - Is the compute budget in the product internally consistent
     across sections?

3. **Conceptual-technical gap check** (Principle 12): cross-
   reference the design against the implementation. Use the
   `gap-audit` subcommand as a formal check if the product has
   not already been gap-audited. Any DIVERGENT-HARMFUL or
   NOT-IMPLEMENTED gap is a Blocking finding.

4. **HPC readiness audit** (when the product involves cluster
   execution):
   - Conda env spec checked in? PyTorch/CUDA versions pinned?
   - Single source of truth for environment variables (one
     `env.sh` sourced by all job scripts, not duplicated
     in-line)?
   - Framework-specific traps (Flash Attention SM compatibility,
     tokeniser offline caching, WANDB offline, DDP collective
     choices, queue QOS limits)?
   - Job dependency graph (no shared mutable state, no bash
     mirrors of application-side path construction, sequential-
     only where required)?
   - Wallclock feasibility (actual step-per-second measurement,
     not estimate)? Arithmetic of stated budgets verified by
     substitution, not copy-pasted from an earlier retrospective?
   - DDP reliability cluster: Gloo-PG for small-tensor collectives,
     fixed-size `all_gather` (never `all_gather_object`),
     master-only save with atomic copy, `NCCL_PROTO=Simple`,
     timeout sized to the longest step. Missing items are
     mutually required: one missing piece causes a hang, not a
     graceful failure.

5. **Repo audit** (file reads only, never executions):
   - Single source of truth: are compute budgets or paths
     mirrored in two places? Mirror drift risk?
   - Branch strategy robustness: git SHA pinning on external
     ports? Ported code carries a header citing origin commit
     + path + line range?
   - Dependency topology: any circular dependency or undeclared
     input?
   - Per-experiment isolation (per-venv, per-branch, per-
     worktree) prevents silent cross-experiment library or
     dataset conflation?

6. **Web search for reference implementations** (mandatory when
   the product claims to port or reproduce external code): find
   the reference repo, read its config and data loader, record
   path + line ranges for any port. Flag undocumented details the
   product missed. Include a reference-implementation-vs-product
   hyperparameter table.

7. **Cross-cutting technical sweeps**:
   - SSOT violations (mirror drift candidates).
   - Unported-with-rationale list: any item on that list turn out
     to be needed after all?
   - Compute arithmetic: verify stated budgets by substitution
     with actual values.
   - Tokenizer and path hygiene: no `/tmp` hardcoding, no
     offline-vs-online mode ambiguity.
   - Figure and rendering audit: consistent colorbar ranges
     across comparison subplots unless caption explicitly
     justifies divergence; visual comparison must not mislead
     by scale.
   - Gradient checkpointing and memory model where applicable.

### Output discovery

Resolution order for the review document path:

1. `--target <path>` if supplied (always wins).
2. `<project-root>/docs/phase<N>-hostile-review.md` where `<N>` is
   the next unused number in `docs/phase*-hostile-review.md`. This
   is the default for projects with a `docs/` directory, matching
   the example-project precedent (`docs/phase2-hostile-review.md`).
3. If `docs/` does not exist and the caller is an orch:
   `~/.claude/comms/<orch-name>/review.md` (the hrev-1 / hrev-2
   precedent).
4. If the caller is meta and no `docs/` exists:
   `~/.claude/plans/<project>/hostile-review-<N>.md`.

Decontamination (see Cross-subcommand conventions) applies when the
path resolves into the project tree.

### Delegation protocol (meta use)

Meta may delegate the review to an orch rather than run it directly:

1. Spin up an orch named `o-<project>-hrev-<N>` (or `-method-<N>` /
   `-tech-<N>` for a `--reviewer-count 2` dual dispatch), following
   the usual meta comms scaffolding.
2. DIR-001 for that orch is the literal `/research hostile-review`
   invocation with the resolved scope, target path, and web-search
   budget. Include the discovered markscheme, spec, and product-
   under-review paths so the orch does not re-discover them.
3. The orch's deliverable is the review document at the resolved
   target path. On completion it reports via RPT and stops.
4. Meta reads the review and issues a subsequent DIR for the fix
   wave, citing the review document as the authoritative punch list.
5. For a two-reviewer dispatch: meta consolidates the two reviews
   manually (preserving per-reviewer attribution, deduplicating
   findings, severity-ranking the merged list). Auto-consolidation
   is not default -- meta adds judgement that a third review
   session would not.

### Output structure (Markdown, `report-technical` conventions)

Per Cross-subcommand conventions (UK English, no emoji,
GitHub-slug anchors). Use the structure below verbatim; sections
may be empty or marked "not in scope for this review" but must
appear in this order so downstream consumers find anchors.

```
# <Project>: Hostile Review (phase <N>)

## 0. Scope and method
<project root | artefacts under review | mode | markscheme discovered
 | spec discovered | prior reviews found | scope flag | reviewer
 count | reviewer identity | date>

## 1. Executive summary
<verdict | top 5 blocking findings | estimated grade band if
 markscheme present | trajectory vs prior reviews>

## 2. Markscheme alignment (if markscheme present and mode B)
<per-tier scoring with point estimate and reasoned band; for each
 checkbox, evidence or gap; total estimate with uncertainty interval>

## 3. Spec coverage audit
<per-requirement coverage table against the discovered spec, status
 key, caveat column, recommended remediation per gap>

## 4. Methodology findings (if --scope includes methodology)
<per-experiment audit table, hypothesis-correctness check, per-
 protocol audit, triangulation audit, decision log audit, novelty
 validation, metric-definition hygiene, bootstrap sensitivity,
 cross-cutting sweeps>

## 5. Technical findings (if --scope includes technical)
<per-code-change audit, per-protocol implementation, gap-audit
 cross-reference, HPC readiness, repo audit, web-search findings,
 cross-cutting sweeps>

## 6. Literature and novelty validation (if novelty is claimed)
<per-claim closest-prior-art list, verdict, preserved delta>

## 7. Extension-novelty audit (mode B only, when extensions exist)
<for each extension beyond the spec: does it strengthen core
 deliverables, or distract? is it a genuine methodological
 triangulation, or decorative? does it violate the core-first
 ordering (Mode B priority 4)?>

## 8. Final verdict
<Blocking / Major / Minor / PASS, one-line summary per item,
 explicit ordering by severity then by remediation effort>

## 9. Remediation sequencing
<ordered punch list: which findings fix which others, which block
 others, suggested atomic directives if the caller is meta>
```

Every finding uses the table form

`| # | severity | location | issue | evidence | recommended fix |`

where `location` is `<file>:<line>` or `<file>:<section>`, evidence
is one of the forms from hostile-specific principle 2, and
recommended fix is a concrete action (not "consider X").

### Workflow

1. Verify maximum effort is in effect (the `w-hostile-reviewer` agent ships `effort: max`); if depth cannot be assured, abort with ESC.
2. Run the project discovery protocol; state project root,
   artefacts, mode, markscheme, spec, prior reviews at the top of
   your working notes.
3. Read the discovered spec in full and enumerate its requirements.
4. Read the product under review in full; cross-reference each
   requirement.
5. Read prior reviews and the decision log; identify what they
   flagged and what was fixed.
6. For each scope flag, run the corresponding gauntlet; keep
   running notes per section of the output structure.
7. Run web searches (methodology: novelty and threshold validation;
   technical: reference-implementation discovery) within the
   budget.
8. Synthesise findings; sort by severity and remediation order;
   write the output document per the exact structure above.
9. Decontamination grep (Cross-subcommand conventions).
10. Report path of the output document back to the caller. If
    caller is meta, state "ready for delegation" and suggest the
    DIR skeleton meta would write for the fix wave.
