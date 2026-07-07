> Part of /research (see ../SKILL.md). Shared substrate: the 15 Principles, inherited by every subcommand.

## Principles

1. **Specificity**: name the exact model, layer range, data split, and
   evaluation mode. "What changes across repeats?" is a conversation starter,
   not a research question. "How does the L2 norm of the residual stream in
   mid-layers 10-21 of the LN-example-project evolve from repeat 1 to repeat 5
   during autoregressive generation on OWT2 validation data?" is a research
   question.

2. **Variable isolation**: identify the independent variable (what we change),
   dependent variable (what we measure), and confounders (what we hold
   constant). Every experiment must isolate exactly one variable; if two things
   change, we learn nothing.

3. **Falsifiability**: state the hypothesis AND what would disprove it. An
   experiment that confirms any outcome teaches nothing. Thresholds in the
   falsification criterion must be numeric, statistically named
   (paired t, Wilcoxon, Mann-Whitney, chi-squared, permutation), and defensible
   -- arbitrary cutoffs are a principle violation (see Principle 3a below).

   *3a. Threshold defensibility*: any numeric cutoff (p < 0.01, CKA > 0.9,
   coverage < 95 percent, correlation |r| < 0.1) must be traced to a primary
   source: a paper, a power analysis, or a published convention. "We chose
   this" is not defensibility. Flag thresholds without provenance as WEAK
   in any review, regardless of whether the threshold happens to be
   satisfied.

4. **Ontological purpose**: for every measurement, answer "what meaningful
   truth does this give us?" before writing code. If the answer is "we can
   implement MLA" that is an engineering purpose; rephrase as "we learn whether
   the latent representations occupy a low-dimensional subspace that compresses
   without information loss, revealing how much of the d=768 capacity the model
   actually uses at each repeat depth."

5. **Confounder elimination**: explicitly list confounders and explain how each
   is controlled. For checkpoint comparisons: same architecture, same data,
   same hyperparameters, different only in the variable of interest.

6. **Experimental triangulation**: every interpretive claim must be supported
   by at least two independent measurements. "Independent" means different
   protocols with different operational definitions -- not the same metric
   computed twice, not the same protocol run on different data, not two
   variants of the same lens projection. When independent measurements
   agree, the claim is "supported"; when only one measurement supports it,
   the claim is "preliminary" and flagged as single-measurement; when they
   disagree, the claim is "inconclusive" and the disagreement itself
   becomes a finding. Design the measurement suite BEFORE the experiment:
   identify which claims need triangulation and which protocols
   cross-validate each other. The cross-validation matrix (mapping claims
   to primary/secondary/tertiary measurements) is a deliverable of the
   experimental design, not an afterthought.

7. **Publication scope discipline**: before designing experiments, establish
   the target scope. If the work is for a publication (conference paper,
   thesis chapter, coursework submission), infer or request from the user:
   (a) the venue and page limit, (b) the core contribution claim (one
   sentence), (c) the time/compute ceiling. Every experiment, measurement,
   and analysis must serve the contribution claim within the ceiling. When
   an investigation "could become an entire project in its own right" (a
   common reviewer/supervisor remark), that is a signal to time-box the
   sub-investigation: set a fixed elapsed-time ceiling, report whatever
   triangulated results exist at the ceiling, and move the rest to "future
   work." Scope discipline is not about doing less; it is about ensuring
   that what is done is complete, rigorous, and publishable within the
   constraint, rather than sprawling and unfinished. When designing a
   new investigation, actively consider: "does this experiment serve the
   contribution claim, or is it interesting but out of scope?" If the
   latter, note it as a backlog item and move on. Scope-shedding triggers
   should be pre-registered: day-N or gate-N conditions that retire a
   sub-investigation automatically rather than by debate.

8. **Codebase forensics for reproduction**: when reproducing prior work, the
   published paper is the specification but the actual codebase is the
   implementation. Papers routinely omit critical configuration details
   (activation functions, gradient clipping values, attention scaling
   flags, tokeniser implementations). Before running a reproduction
   experiment, read the reference codebase's config files, data loaders,
   and model definitions. Document every divergence from the paper's
   description. Undocumented details discovered through code reading are
   findings in their own right and should be reported.

9. **Sweep design for confounders**: when a variable might confound the
   comparison (model width, learning rate, number of layers), sweeping it
   across multiple values is strictly more informative than picking a single
   point. A sweep isolates the confounder as a controlled variable, reveals
   thresholds and scaling laws, and naturally provides matched comparisons
   (e.g., a width sweep gives param-matched pairs between adjacent widths).
   When the compute budget permits, prefer a sweep over a single-point
   experiment with a post-hoc justification for the chosen value.
   Monotonicity claims require dense sampling near the turning point: before
   asserting "the quantity is monotone in X" or "crosses threshold T at
   X = x*", verify that the sweep resolution is finer than the binomial or
   bootstrap CI width near the turning point. Sparse sweeps with loose CIs
   cannot support monotonicity claims.

10. **Validation gates**: before running a full experiment, confirm that the
    baseline achieves non-trivial performance. If the baseline fails (e.g.,
    a 4-layer Transformer cannot count at a given model width), the
    comparison is uninformative regardless of the treatment's performance.
    A validation gate is a pre-registered threshold (e.g., >= 80 % IND
    accuracy) that the baseline must pass before the full experiment
    proceeds. Gates prevent wasted compute on uninformative comparisons and
    should be stated in the experimental design, not discovered after the
    fact. Validation gates are NOT hypothesis tests: they are pre-conditions
    that abort the experiment if false. They are therefore exempt from the
    multiple-comparisons family and not counted in the statistical-test
    ledger. For symbolic derivatives and closed-form algebraic results,
    an autograd gate (finite-difference vs autograd, max relative error <
    1e-9) is a cheap numerical validation that catches sign errors and
    missing terms before downstream code depends on them.

11. **Pre-registration**: predictions, thresholds, and falsification criteria
    are recorded BEFORE the experiment runs, in a pre-registration artefact
    (decision log, experiment design doc, or planned-analysis document). A
    decision log uses explicit `DEC-NNN` (or equivalent project-scoped)
    entries, each carrying a decision, value, rationale, and -- where
    applicable -- a contingency trigger describing under what condition
    the decision would be revisited. Post-hoc findings that were not
    pre-registered are flagged as exploratory and require a fresh experiment
    to confirm; they cannot be promoted to confirmatory status by the
    same data that discovered them. Pre-registration is not bureaucracy;
    it is how we tell confirmation apart from discovery.

12. **Conceptual-technical gap closure**: every project has two layers
    that must remain in correspondence -- the *conceptual* design (what
    truth we seek, which variables, which controls, which thresholds,
    which metrics) and the *technical* implementation (code, libraries,
    runtime, models, file layout, render pipeline). The gap between them
    is a silent failure mode: the design says one thing, the code does
    another, and both are internally consistent so nobody notices. Every
    phase of a project must produce an explicit mapping from conceptual
    to technical and every review must verify that mapping holds. The
    `gap-audit` subcommand operationalises this check; the discipline
    also lives as a cross-cutting principle across all other subcommands.
    Hypothesis-formulation errors (the claim is mathematically wrong
    under the null) are a special case: before collecting data, derive
    the unconditional distribution of the primary observable under H0.
    If the derivation says E[X] = 0 for all values of the manipulated
    parameter, do not hypothesise "X will be positive when parameter > 0."

13. **Dependency-DAG discipline**: maintain an explicit graph of the
    computational artefacts (notebook cells, scripts, configs, figures,
    tables, report sections) and their input-output dependencies. Any
    re-run of an upstream artefact (dataset generator, hyperparameter
    optimiser, seed change, library upgrade) requires re-execution of
    every downstream artefact; numbers cited in prose must be re-verified
    against the latest source of truth, not copied from earlier reports
    or retrospectives. For multi-cell notebooks, a DAG comment block at
    the top; for multi-script projects, a `Makefile`, `dvc.yaml`, or
    equivalent artefact-dependency graph. Dataset-realisation shifts are
    the canonical cascade: one upstream change can invalidate dozens of
    downstream numbers without raising any exception.

14. **Verdict-language calibration**: strong verdict keywords -- "confirmed",
    "monotone", "dominates", "converges", "resolves", "invariant" -- are
    reserved for claims backed by multi-seed replication AND independent
    triangulation. Single-seed or single-metric observations use hedged
    language: "observed", "consistent with", "suggestive of", "preliminary
    under N=1 seed". Verdict miscalibration is a principle violation even
    when the finding is ultimately correct, because it misleads a reader
    about the epistemic confidence behind the claim.

15. **Literature as confounder**: in novel work, what you do not know
    about the literature IS a confounder. A method that looks original
    may be standard in a neighbouring subfield; a finding that looks
    unprecedented may be Table 3 of a 2023 survey. Systematic literature
    search is not optional: every claim of novelty is validated with
    targeted searches and recorded as NOVEL / PARTIALLY-SCOOPED /
    SCOOPED with the preserved delta where partial. Citation expansion
    defers until the technical scope narrows; wide pre-scope searches
    waste time on irrelevant subfields, so `literature` is typically
    invoked after `design` has produced a concrete contribution claim.
