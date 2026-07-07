> Part of /research (see ../SKILL.md). Subcommand: design.

## `design` -- Design a mechanistic investigation

**Use when**: starting a new investigation of a computational
phenomenon. Produces a complete experimental design with every
Principle explicitly addressed. Inherits the `--deep` quality that
was the former modifier; the standard is the only standard.

**Args**: `design <phenomenon> [--model <variant>] [--scope <layers|repeats|heads>]`

1. **Phenomenon identification**: what observable behaviour or pattern are we
   trying to explain? State it as a concrete observation, not a vague area.
2. **Prior knowledge**: what do we already know from the codebase, training
   logs, existing analyses, and project memory?
3. **Hypotheses** (minimum 2, ideally competing): formulate testable
   predictions. Include null hypothesis. Pre-register each hypothesis with
   a quantitative falsification threshold and named statistical test
   (Principle 11). Hypotheses are recorded in a decision log entry
   (`DEC-NNN`) BEFORE experiments run.
4. **Experimental design**: for each hypothesis:
   - Independent variable (what we manipulate or observe varying)
   - Dependent variable (what we measure, with units and precision)
   - Controls (what must be held constant, with specific values)
   - Confounders (what could invalidate the result, and how we prevent it)
   - Data requirements (which split, how many samples, why that number;
     power analysis or empirical justification required)
   - Compute budget (GPU-hours, wall time)
5. **Measurement protocol**: exact metrics, aggregation method, statistical
   tests. No "we'll look at the plots." For hand-derived symbolic results
   (closed-form gradients, Hessians, derivations), add an autograd gate
   comparing finite-difference vs autograd derivative with max relative
   error < 1e-9 (Principle 10). Define all metric names with their sign
   convention, limiting cases, and ground-truth-vs-noisy distinction
   BEFORE running: regret vs pseudo-regret vs inference loss are not
   interchangeable, and the definition chosen after the experiment is
   not a definition.
6. **Interpretation guide**: for each possible outcome, what truth do we
   learn? What follow-up question does it generate? Strong verdict
   language is reserved for multi-seed triangulated claims
   (Principle 14); the interpretation guide states in advance which
   outcomes permit which verdict tier.
7. **Triangulation plan**: for each claim the experiment might support,
   identify which other protocols cross-validate it (Principle 6). Build
   the cross-validation matrix as part of the design, not after results.
   Audit the operational independence of each pair: two measurements that
   share a forward pass AND share a derived statistic are a single
   measurement; record that explicitly.
8. **Multiple-comparisons discipline**: count the statistical tests in
   the design. For pre-registered primary hypotheses across research
   questions, apply family-wise control (Holm-Bonferroni at alpha=0.01
   is a sensible default). For secondary exploratory contrasts, apply
   Benjamini-Hochberg FDR (q=0.05). Validation gates (Principle 10) are
   NOT in the family -- exempt them explicitly.
9. **Scope check**: does this investigation serve the publication's
   contribution claim (Principle 7)? Include pre-registered
   scope-shedding triggers (day-N or gate-N) that retire the
   investigation if a pilot fails.
10. **Codebase forensics** (when reproducing prior work): read the
    reference implementation's config, data loader, and model code
    (Principle 8). Document undocumented details as findings. When
    reproducing AND extending, consider a twin-pilot design:
    Pilot 1 = reproduction-faithful (match the reference exactly);
    Pilot 2 = fair-comparison (use native defaults for your
    architecture). The contrast isolates the training-regime effect
    from the architecture effect and pre-empts reviewer pushback on
    both fronts.
11. **Validation gate**: define a pre-registered baseline performance
    threshold below which the comparison is uninformative
    (Principle 10).
12. **Conceptual-technical mapping**: for each design decision, state
    the concrete technical realisation (which file, which function,
    which config flag). The `gap-audit` subcommand will subsequently
    verify the mapping holds (Principle 12).
13. **Report format**: table of results, key figures, one-paragraph
    synthesis. Deliverable location follows the project's
    decision-log convention.
