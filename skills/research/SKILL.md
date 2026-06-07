---
name: research
description: "Scientific research: design, literature, hostile-review, report, and friends."
category: domain
user-invocable: true
disable-model-invocation: true
argument-hint: "<design|paper|literature|ablation|hyperparams|reproduce|gap-audit|hostile-review|report|report-publish|report-technical> [args]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# Scientific Research

**Usage**: `/research <subcommand> [args]`

Every subcommand inherits the Core Ethos, the 15 Principles, and the
Cross-subcommand Conventions below. These are the ubiquitous quality bar:
no subcommand operates at a lower standard. The former `--deep` modifier
is retired because it described the only standard we use.

## Core Ethos

We are computer scientists applying the scientific method to computation. The
goal is to **discover truth**: the theory, functioning, training/working
dynamics, explanations for phenomena. Understanding is the primary outcome;
engineering improvements are consequences, not goals.

Every research question, experiment, and report must satisfy the Principles
below. When a principle's cost feels high, the principle still holds --
cost is a signal to pick a smaller scope, not a weaker standard.

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

## Project modes

Every `/research` invocation operates in one of two modes. Mode is
discovered at startup and determines which principles dominate the
interpretation of success. Both modes enforce all 15 Principles; the
mode only determines which knob is primary when trade-offs are required.

### Mode A -- Publishing novel work

No official markscheme. Truth is the grading axis. A referee, a thesis
examiner, or a future-you reading the paper is the reviewer. The four
Mode A north-stars:

1. **Epistemological certainty**: every claim is either mathematically
   proven, empirically triangulated (Principle 6), or flagged as
   preliminary with explicit uncertainty quantification. No "probably",
   "seems to", "appears that" without a named uncertainty interval.

2. **Literature depth**: systematic search covering foundational works
   (pre-2010 seminal papers for the subfield) AND recent surveys and
   primary papers (prefer 2023-2026) to identify both the established
   body and the active research front. Cite both when substantiating a
   method or formalism (Principle 15). Novelty claims are audited via
   `literature` and `hostile-review`.

3. **Hostile-review defensibility**: the project passes a two-scope
   hostile review (methodology + technical) before the next commit
   horizon -- paper submission, arxiv post, thesis-chapter sign-off.
   Budget 2-3 review passes: each pass tends to surface a different
   severity tier (pass 1 catches ~70 percent of big issues, pass 2
   finds subtler tier-1 and polish issues that only surface once the
   first-pass fixes are in, pass 3 confirms the fixes held).

4. **Curiosity-driven creativity**: out-of-the-box experiments are
   encouraged; the weirdest experiment has the highest chance of
   revealing a non-obvious truth. Creativity is constrained by
   Principle 7 (scope discipline) and Principle 10 (validation gates)
   but not by conventionality. A well-gated speculative experiment is
   a Mode A asset.

### Mode B -- Working toward an assignment

A markscheme, specsheet, or syllabus with learning objectives exists.
Markscheme alignment is the grading axis; novelty is welcome as
extensions. The four Mode B priorities:

1. **Markscheme alignment**: map every rubric checkbox to evidence in
   the submission. A tier whose checkboxes are unsupported loses
   points regardless of how impressive the uncorrelated work is.
   When an official markscheme leaves ambiguity (grade-A criteria are
   implicit or under-specified), construct an *augmented markscheme*
   that wraps the official one inside a superset of rigour dimensions
   (theory / technical / methodology / interpretation / presentation)
   with explicit weights and -- critically -- *penalty modifiers*
   (page-overflow, firewall leak, hallucinated citation) applied
   after raw dimension scoring. Store the augmented markscheme where
   the review subcommand will find it: `plans/<project>/markscheme.md`.

2. **Specsheet fidelity**: the spec defines the problem boundaries
   (formulas, inputs, outputs, required plots, page budget). Every
   specsheet bullet is a line item on a coverage audit. Auto-corrections
   and "nicer" variants are scope-creep unless the spec explicitly
   permits them; deviations must be justified in the report.

3. **Learning-objective coverage**: the syllabus (lecture slides, unit
   objectives, textbook chapters) defines the concepts the assignment
   implicitly tests. Coverage should anchor to specific lecture
   references AND textbook sections. A local `docs/book-index.md`
   mapping textbook chapters to page numbers prevents "deep learning
   says X" vagueness -- every external reference is checked against
   the book's own ToC before it enters the report.

4. **Extension novelty (optional, valuable)**: once the core
   deliverables are covered, a well-chosen extension -- a new question
   the assignment does not ask, a sensitivity analysis of an
   assumption, a methodological triangulation of an assumed result --
   is a grade-band lever that shows deeper understanding. Extensions
   without core-deliverable completion are a negative signal; the
   order is never reversible.

### Mode detection and override

Mode is inferred from startup discovery:

- If a markscheme artefact is discovered (per the discovery protocol
  below), mode defaults to B.
- If no markscheme is discovered, mode defaults to A (stricter).
- Any subcommand accepts `--mode A|B` to override the inferred mode.
- When mode is genuinely ambiguous (a project is partly a thesis chapter
  and partly a coursework artefact, for example), default to A and
  treat markscheme alignment as an additional constraint on top of
  A's rigour knobs.

## Project discovery protocol

The skill does not hard-code per-project paths. Every subcommand that
operates on an existing project context (all of them except `paper`
when `--in` is explicit) runs this discovery protocol at startup.
State each resolution explicitly at the top of the subcommand's output
or working notes.

1. **Project root**: the nearest ancestor directory of the caller's
   CWD that contains a git repository, a `docs/` folder, or a
   `~/.claude/plans/<name>/plan.md` entry.

2. **Project memory**: query the DB for accumulated traps: `HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python ~/.claude/scripts/memory/memory_db.py search '<name> gotchas mistakes wins' -k 8` or `list --tier shared-projects`. A review, design, or gap audit is the right moment to verify none have been silently re-introduced.

3. **Markscheme**:
   - Prefer `~/.claude/plans/<project>/markscheme.md` (the current
     convention -- example-project, example-course-labs).
   - Fall back to `<project-root>/docs/markscheme.md`.
   - Fall back to `--markscheme <path>` if supplied.
   - If none found, mode defaults to A; state "no markscheme
     discovered" and apply only the principle-level gauntlet. Do
     not invent tier weights.

4. **Spec or syllabus**:
   - Look in `<project-root>/docs/*.pdf` and `<project-root>/docs/*.tex`
     for the assignment brief, call-for-papers, or task specification.
   - Fall back to `--spec <path>` if supplied.
   - Read in full. Enumerate explicit requirements as a checklist
     the subcommand will audit coverage against.

5. **Product under review**:
   - For coursework: the report (`.tex` or `.md`), the notebook
     (`.ipynb`), any accompanying code modules.
   - For a research project: the design document
     (`extend-notes.md`, `plan.md`, `experiments.md`), the code
     under analysis, the reference paper.
   - For a reproducibility study: the reproduction repo, the
     divergence log (`reprod-notes.md`), the paper PDF.
   - State the enumerated product list explicitly; subsequent
     findings must cite locations within this set.

6. **Prior reviews and decision logs**:
   - `<project-root>/docs/phase*-hostile-review.md`,
     `<project-root>/docs/*review*.md`,
     `~/.claude/comms/*hrev*/review.md`.
   - If a decision log exists (in `state.md`, `context.md`, or a
     dedicated `decisions.md`), read it to understand the
     pre-registered choices the subcommand must respect.
   - When prior reviews exist, your first question is what they
     missed. Do not re-raise a finding already addressed unless
     you have new evidence that the fix was incomplete.

7. **Output path and mode**: resolve the output path per the
   subcommand's conventions; resolve mode per the detection rules
   above.

## Cross-subcommand conventions

These apply to every subcommand that writes artefacts. Subcommands
reference this section rather than restating.

- **Output format**: subcommands producing Markdown artefacts follow
  `report-technical` conventions (GitHub-slug anchors, tables for
  structured data, code-line citations, named methodological
  subsections when a concept is referenced 2+ times). Subcommands
  producing LaTeX artefacts follow `report` conventions (layered
  information, notational coherence, appendix-based derivations).

- **Web-search budget**: subcommands that require literature work
  accept `--web-search-budget <N>` (default 10-15). Do not exceed
  the budget. Prioritise by expected information gain: novelty
  claims and threshold provenance first, reference implementations
  second, wildcard reserves last. Each search: cite URL + title +
  one-line takeaway, time-boxed at 2-3 minutes per search.

- **Decontamination**: when the output path resolves into a
  project directory (`<project-root>/...`), grep the draft for
  superclaude identifiers before writing: `~/.claude/`,
  `agent-memory`, `MEMORY.md`, `mtm.md`, `ltm.md`, `.memory.db`,
  `.comms.db`, `.broker.db`, `memory_db.py`, `comms_db.py`,
  `MM-\d+`, `GM-\d+`, `M-\d+`, internal orch names, internal
  project-memory filenames. Paraphrase hits as external
  references before save. Paths under `~/.claude/` are exempt
  from decontamination but should still paraphrase where portable.

- **Rendering hygiene**: UK English throughout (`behaviour`,
  `analyse`, `optimise`, `fibre`). No emoji. No ASCII emoticon
  triggers that GitHub auto-substitutes (`<3`, `:)`, `:D`) --
  write "less than 3" in words, or insert a space between the
  less-than sign and the digit. For LaTeX outputs: no em-dashes,
  `\paragraph{...}` not `\textbf{...}` as headings, `\cref{...}`
  not raw "Section 3", consistent math fonts (`\mathbf` for
  matrices). For Markdown outputs: no trailing spaces, fenced
  code blocks with language tags, every in-file link resolves
  to a real header slug.

- **File-based analysis only**: the skill reads files and executes
  analysis tools (`grep`, `ls`, language-level parse). It does NOT
  execute the code under review, run training, submit HPC jobs, or
  mutate git state beyond local reads. Reading data files is fine;
  running the thing being studied is not.

- **Every claim cites evidence**: a file:line reference, a
  literature citation with arxiv ID or DOI, a numerical re-run
  with expected-vs-actual, or a principle name with the clause it
  violates. Opinions without evidence are dropped, not downgraded.

- **Respect project git policy**: check for `/push false` or
  equivalent before proposing commit steps. When the policy is no
  agent commits, remediation sequences describe changes for a
  human to commit manually.

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

## `paper` -- Find implementation of a paper

**Args**: `paper "<title>" [--in <path>]`

1. Grep project for paper title, author names, arxiv IDs, key method names
2. For each match: which concepts implemented, how faithfully, deviations noted
3. Output: `| File | Lines | Concept | Faithful? | Notes |` + "Not Found" list

## `literature` -- Systematic literature survey

**Use when**: starting a novel research project (Mode A), identifying a
research gap, mapping prior art after a contribution claim is drafted,
or validating novelty claims in an already-drafted project. This is
distinct from `paper`, which locates the implementation of a single
known paper. Use `literature` when the topic is "active inference in
neural ODE posteriors" and you need to map the field; use `paper` when
the target is a single known title.

**Args**: `literature --topic "<phrase>" [--target <path>] [--web-search-budget <N>] [--years <YYYY-YYYY>] [--anchors <textbook-or-survey>]`

### Principles specific to `literature`

Inherits top-level Principles 1-15 (Principle 15 is the spine).
Literature-specific additions:

- **Foundational + front distinction**: every subtopic is anchored by
  at least one foundational work (pre-2010 seminal paper for the
  subfield) AND at least one recent primary or survey paper (prefer
  2023-2026). A survey citing only one stratum is incomplete.

- **Gap-oriented output**: the deliverable is a table of *gaps*, not
  a list of papers. A gap is a question the literature does not yet
  answer coherently (papers disagree, untested at scale, no
  transfer to adjacent domains). Gaps feed `design`.

- **Deferred expansion** (Principle 15 corollary): invoke
  `literature` AFTER `design` produces a concrete claim that
  narrows the search surface. Pre-design literature is acceptable
  only when the discovery of a contribution claim is itself the
  deliverable.

- **Provenance per citation**: arxiv ID / DOI / venue-and-year
  triplet plus one-line takeaway. Paywalled papers marked
  "abstract only". Vague "the literature says" paraphrase is a
  principle violation.

### Workflow

1. Run the project discovery protocol. Resolve the mode (A typically
   for `literature`).
2. Seed the search with the topic plus synonyms; bias to 2023-2026
   where appropriate (recent front) and separately search
   foundational-years for the subfield's pre-2010 anchors.
3. Optionally use `--anchors` to specify a textbook or survey whose
   bibliography is the natural starting graph.
4. For each result: title, authors, venue, year, arxiv/DOI,
   one-line takeaway, relevance verdict (central / adjacent /
   tangential). Drop the tangential before final write.
5. Build a citation graph: foundational works + recent surveys +
   ancestor-candidates + contemporary peers. Note disagreements
   between papers explicitly.
6. Identify gaps: where does the literature disagree? What
   questions has no paper yet answered coherently? Which claims
   rest on a single paper with no independent replication? Each
   gap is a candidate research question.
7. For assignment mode (B), if the task involves a literature
   section, align the gap framing with the assignment's learning
   objectives and cite lecture slides + textbook sections
   alongside primary sources (Mode B priority 3).
8. Output a Markdown artefact per `report-technical` conventions.
   Default target: `<project-root>/docs/literature-<topic-slug>.md`;
   fall back to `~/.claude/plans/<project>/literature-<topic-slug>.md`.
   `--target` overrides.

### Output structure (Markdown, `report-technical` conventions)

```
# Literature survey: <topic>

## 0. Scope
<search topic | date window | anchors | web-search budget used | date>

## 1. Foundational works
<table: title | authors | year | venue | takeaway | role in the field>

## 2. Recent front (2023-2026)
<table: title | authors | year | venue | arxiv/DOI | takeaway | relevance>

## 3. Citation graph and disagreements
<paragraphs on how papers cite and contradict each other>

## 4. Gaps and research-question candidates
<table: gap | evidence for the gap | candidate RQ | suggested protocol>

## 5. Novelty verdicts (if a contribution claim was provided)
<per claim: NOVEL / PARTIALLY-SCOOPED / SCOOPED; preserved delta>
```

## `ablation` -- Design ablation study

**Args**: `ablation <component> [--baseline <EXP-ID>] [--budget <N>]`

1. Read component code + `experiments.md` for baseline
2. Identify parameters and sub-components
3. For EACH ablation: state the hypothesis, the isolated variable, and
   the confounder controls (not just "change X and see what happens").
   Inherits `design`'s pre-registration and triangulation requirements
   (Principles 6, 11) -- an ablation without a pre-registered hypothesis
   and a triangulation plan is exploratory, not ablative.
4. Output: `| # | What to Change | Hypothesis | Config Change | Priority |`
5. Budget (default 5) limits runs. Prioritize by information gain.
6. Include compute estimate (runs x time per run)

## `hyperparams` -- Compare against paper

**Args**: `hyperparams [--paper "<title>"] [--config <path>]`

1. Extract current hyperparams from config files
2. Compare against paper defaults
3. Output: `| Parameter | Current | Paper | Match? | Notes |`
4. For each deviation: explain whether it affects conclusions

## `reproduce` -- Check reproduction fidelity

**Args**: `reproduce <paper> [--strict]`

1. Read architecture, training loop, data pipeline
2. Build checklist: Architecture, Training, Data
3. Score: X/Y match per category
4. Flag critical gaps + acceptable deviations
5. `--strict` flags ALL deviations; default only critical gaps
6. **Divergence log**: cross-reference `docs/reprod-notes.md`. Each entry:
   what changed, why, and impact assessment.

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

## `hostile-review` -- Adversarial second-reviewer pass

**Use when**: a first draft exists and you need a truth-seeking stress
test before the next commit horizon -- coursework submission, HPC
wave, paper submission, phase gate. The subcommand is deliberately
adversarial: it catches what self-review missed by assuming the
weakest interpretation of every claim and demanding evidence rather
than plausibility.

**Args**: `hostile-review [--scope methodology|technical|both] [--target <path>] [--reviewer-count 1|2] [--web-search-budget <N>] [--markscheme <path>] [--spec <path>] [--mode A|B]`

**Execution mandate**: always run with **ultrathink AND /effort max**
engaged. This subcommand is disabled without both. If invoked from a
context where either is unavailable, escalate rather than proceed.

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

1. Verify ultrathink + /effort max engaged; abort with ESC if not.
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

## `report` -- Academic LaTeX research report (non-publishing default)

**Use when**: producing or refining a LaTeX academic report for a
marker, supervisor, or internal audience -- coursework submissions,
project memos, handoff documents, thesis-draft chapters read only
inside the group. Defaults to Principle 9 economy: textbook results
are invoked by name and citation, derivations are reserved for
project-specific material. Use `report-publish` for external peer
review (conference papers, journals, thesis chapters with external
examiners); use `report-technical` for Markdown documentation.

**Args**: `report <section|full> [--ref <notation-reference.pdf>] [--audience <level>]`

Produces or refines a LaTeX research document structured for
layered comprehension: the main body introduces and contextualises;
appendices provide the derivations that are genuinely specific to
the project (Principle 9).

### Setup (Anti IDE / VS Code on WSL, one-time per project)

Before the first save of any LaTeX file in a subdir (e.g. `sections/`),
disable `latexindent` format-on-save at the workspace root. The
latex-workshop extension (10.13.1) invokes
`latexindent -c <root-dir>/` but then cleans up
`<file-dir>/indent.log` -- a path mismatch that throws `ENOENT` on
every Ctrl+S for files outside the root dir. The build itself
succeeds, but the noisy error masks real failures and pollutes the
output channel. A second adjacent failure mode: a Ctrl+S during an
in-flight latexmk run can leave `main.aux` corrupted with null bytes
(`./main.aux:1: Text line contains an invalid character`); recovery
is `latexmk -C && latexmk -pdf` from the directory containing
`main.tex`, but the root cause is the same auto-formatter race that
this setup defuses.

Mitigation: create `.vscode/settings.json` at the workspace root (the
folder the IDE opens, which is what `%WS1%` resolves to in the LaTeX
Workshop output channel) with:

```json
{
  "[latex]": { "editor.formatOnSave": false },
  "latex-workshop.latex.autoBuild.run": "onSave"
}
```

Reload the window (`Ctrl+Shift+P -> Developer: Reload Window`) after
creating; save-time settings do not retroactively apply, and saves
made between settings creation and reload still trigger the bug.
Auto-build on save is preserved, so the PDF still updates on Ctrl+S;
manual indentation stays available via
`Ctrl+Shift+P -> Format Document`. If the IDE was opened at a
parent of the LaTeX project (e.g. workspace root is `example-course/` but
the report lives at `example-course/iqc/report/`), place the
`.vscode/settings.json` at the IDE's workspace root, not at the
report's parent. Verify the right level by reading the LaTeX
Workshop log: the path component immediately following `%WS1%/` is
the first directory inside the workspace (so `%WS1%/report/` means
workspace root contains `report/` directly). User-level Antigravity
settings can override workspace settings; if the bug persists after
a reload, mirror the same block into
`Ctrl+Shift+P -> Preferences: Open User Settings (JSON)`. Applies to
Anti IDE and VS Code on WSL; native macOS/Linux installs may not
exhibit the path mismatch but the setting is harmless there.

### Principles

1. **Layered information architecture**: the main body conveys what a
   concept IS, why it matters to the research, and how it connects to
   adjacent sections. The appendix conveys the full mathematical
   machinery (proofs, derivations, step-by-step expansions). Every
   non-trivial equation in the main body must cite the relevant
   appendix section with `(see Appendix~\ref{app:...} for derivation)`.

2. **Audience calibration** (`--audience`, default `meng-cs`):
   derivation thoroughness scales to the audience. For `meng-cs` (CS
   students with foundational calculus and linear algebra): tensor
   products need brief motivation but not axiomatic construction;
   TDVP needs every intermediate step. The test: "would an CS
   student follow this without external references?" If no, expand.

3. **Notational coherence**: adopt a single notation reference
   (`--ref`) and enforce it throughout. Track all symbols in a
   `\newcommand` preamble block. Never introduce two symbols for the
   same object. When the reference uses `|a\rangle` for kets and
   `\langle a|b \rangle` for inner products, every section must
   follow suit. Notational decoherence (mixing conventions across
   sections) is a hard error.

4. **Citation discipline**: every derivation that originates from
   another work must cite it at first appearance. Foundational
   results (e.g., spectral theorem) cite a textbook; recent methods
   (e.g., NQS architectures) cite the original paper AND the most
   recent survey (prefer 2023-2026). Never present someone else's
   derivation as original work, even when re-derived.
   Citation-integrity is a grading-worthy dimension: every external
   reference is verified against the textbook's ToC or the primary
   source before it enters the report, and citation hallucinations
   are a penalty in Mode B (augmented markscheme modifiers) and a
   principle violation in Mode A.

5. **Section separation of concerns**: Introduction introduces
   (scope, motivation, structure), Background defines (formalisms,
   axioms, tools), Literature Review analyses (surveys, compares,
   selects), Conclusion synthesises (findings, limitations, next
   steps). Content must not leak across these boundaries. A
   Hamiltonian definition belongs in Background; its application to
   a specific QC platform belongs in the Literature Review.

6. **Formalism coherence**: choose ONE dynamical picture
   (Schrodinger, Heisenberg, or interaction) and ONE algebraic
   convention set at the start. Document the choice with a brief
   justification. Enforce it across all sections and appendices.
   Mixed formalisms are a hard error unless explicitly justified
   (e.g., switching to interaction picture for perturbation theory,
   with a clear transition statement).

7. **No bullet points in academic prose; `\paragraph` headers are
   opt-in**: enumerated lists are acceptable for algorithms, phase
   plans, and explicit multi-part definitions. All other content is
   continuous prose. `\paragraph{...}` headers are opt-in
   substructure -- default off -- reserved for literature reviews
   and extended-survey sections where sub-claim navigation aids the
   reader. For standard body prose, appendix derivations, and
   coursework sections, flowing prose without paragraph headers is
   preferred unless the caller explicitly requests
   literature-review-style substructure.

8. **Writing-line seed discipline** (useful for scaffolding before
   prose): for each subsection, a three-line seed -- L1 headline
   claim (one-sentence mathematical statement or empirical
   quantification with numbers), L2 non-obvious observation
   (counter-intuitive detail that differentiates a smart essay
   from a generic one), L3 theory link (bridge to next section or
   cited reference) -- prevents prose from drifting into pure
   engineering ("we ran the code") or pure theory ("the literature
   says"). The seed lines are recorded as `%`-comments in the
   LaTeX source; the author expands them into prose. The pattern
   is not mandatory but is the recommended scaffolding for
   mode-B (assignment) reports and for first-draft paper sections.

9. **Invocation-by-reference economy** (non-publishing default):
   markers and supervisors reading parallel submissions score
   elegance over exhaustive rederivation. Textbook results carrying
   no project-specific novelty -- Mercer's theorem, the kernel
   trick, Sherman-Morrison-Woodbury, Cholesky factorisation,
   standard MVN marginals, closed-form KL between canonical
   distributions, the representer theorem -- are invoked by name
   plus a textbook citation, not re-derived. Full-derivation budget
   is reserved for what is GENUINELY SPECIFIC to the project:
   moments of a custom dataset, a kernel specialisation the
   argument turns on, an identity bridging two non-standard
   quantities. Test: "is this derivation available in the cited
   reference with minor notational change?" If yes, invoke. Prefer
   synthesis sentences linking the invoked result to the
   project-specific claim over pedagogical recaps. Use
   `report-publish` for peer-reviewed contexts (conference papers,
   journals, theses) where reviewers expect full derivations even
   of textbook results; there this principle is suspended.

10. **Spec-first delivery posture**: each subsection opens with the
    direct answer to the spec question in one or two sentences,
    before any derivation, definition, or citation. The reader
    should know the verdict from the first sentence; mathematical
    justification supports it, not announces it. Violation: opening
    with "we derive X" or "first we recall that Y". Pass: "Three
    criteria reduce each shortlist from four to two plausible
    kernels per dataset (Table N)." A section that needs two
    paragraphs of preamble before the spec-question answer has
    mis-scoped its content: the preamble belongs in a prior
    section, an appendix, or is cuttable. Answer posture is a
    readability lever and a marker-friendliness lever: markers
    scoring many submissions decide in the first sentence whether
    to read closely or skim, and an answer-first opening earns
    that decision.

11. **Intuition before formalism**: within a body paragraph, visible
    structure (a figure reading, a physical analogy, a layman
    paraphrase) precedes the equation. The equation formalises what
    the reader already grasped; it does not introduce the claim.
    Violation: leading with an equation whose meaning is unpacked in
    the following sentences. Pass: "Edge-to-centre amplitude triples
    for \Dlin in Fig. N; the ratio [eq] quantifies what the eye
    already reads." For each non-trivial formal object, include a
    one-sentence layman paraphrase demonstrating intuitive command
    ("a smoother process stays correlated at larger lags"). The
    paraphrase is not padding: it is the evidence that the author
    understands the math, not merely transcribes it. Applies
    symmetrically to figures: prose describes what the reader can
    see before introducing the metric that scores it.

12. **Interest filter for statistics**: raw summary statistics
    (means, standard deviations, per-dataset listings) earn
    inclusion in body prose only when (a) the reader needs the exact
    number to verify a claim, or (b) the number itself is
    counter-intuitive. Default to an ordering or a ratio
    ("\kOU and \kosc are \(\sim 1.5\times\) jaggier than \kSE and
    \klin") over a listing ("R = 2.41, 3.36, 3.94, 2.23"). Listings
    belong in figure captions, appendix tables, or the source
    notebook. Every statistic in body prose must be wrapped in a
    claim about ordering, deviation, anomaly, or threshold crossing;
    a bare number without a claim is a stranded statistic and gets
    cut. The interest filter is not about suppressing data: it is
    about forcing each number to pay the claim it earns a place to
    state.

13. **TOUCH-zone edit protocol (author-declared lockdown)**: before
    any edit on a LaTeX file, run `grep -nE "% TOUCH HERE|% STOP TOUCH"
    <file>` to enumerate paired markers. If any pairs are present,
    ALL agent edits MUST occur strictly between matching pairs; text
    outside any TOUCH zone is immutable to the agent regardless of
    how tempting or minor the fix appears -- the markers declare
    content the author wants preserved verbatim. If the requested
    edit requires modifying text outside every TOUCH zone, stop and
    surface the conflict in chat: describe the needed out-of-zone
    change, draft the replacement as terminal output, and wait for
    the author to move the markers or integrate the edit. When no
    markers are present, no lockdown applies and the parent
    workflow's edit scope governs. Tolerate common marker typos
    (e.g. `TUOCH`, missing leading `%`) by flagging them in chat
    and respecting the intended bounds rather than silently
    treating the typo-marker as absent. This protocol composes with
    parent write-scope rules: TOUCH zones narrow the edit surface,
    they do not widen the agent's authority.

14. **Complexity calibration (the spice rule)**: technical depth in
    prose is like spice -- every reader has a different tolerance,
    but over-seasoning is universal. The remedy is not to remove
    complexity but to calibrate it: every formal object that earns
    its place in body prose must (a) be load-bearing for the claim
    being made, (b) be paired with an intuitive paraphrase before or
    immediately after the formalism (Principle 11), and (c) have a
    single, named role -- never decorative. When in doubt, push the
    derivation to the appendix and keep the body about what changes,
    why it matters, and what the reader sees. The aim of `--report`
    prose is to make the work **meaningful and understandable**, not
    to demonstrate everything the author knows. If a writing-line
    triple has five ideas and the target word count is 130 words,
    choose the two or three that carry the most epistemic weight;
    appendix or cut the rest. Mode-B markers reading parallel
    submissions reward this explicitly: a 2-minute insight
    out-scores a 10-minute subscript slog at equal information
    content. Divulgation -- the academic-prose discipline of making
    a technical claim accessible without dumbing it down -- is part
    of the standard, not a softening of it. The principle is
    mode-aware: in publishing contexts (`report-publish`, Principle
    9 suspended) the body absorbs more complexity by necessity, but
    the calibration target shifts to "complete and rigorous to a
    specialist reviewer", not "maximally seasoned".

15. **Concept-Interpretation-Application discipline (the divulgation
    arc)**: each load-bearing technical concept must follow a
    structured three-move arc within or across paragraphs:
    (i) **introduce** the concept with its formal statement or a
    brief derivation; (ii) **interpret** it in plain English with a
    one-sentence layman gloss demonstrating intuitive command;
    (iii) **apply** the interpreted concept to read an experimental
    result or metric, comparing RELATIVE magnitudes (orderings,
    ratios, gaps, deltas) rather than reciting absolute numbers.
    Exact numbers belong in figures, captions (≤2 rendered lines),
    or tables -- body prose reads them, doesn't restate them.
    Implementations and hyperparameters of numerical results belong
    in the appendix. The (i)→(ii)→(iii) triplet demonstrates
    ownership of the concept; partial executions read as
    transcription. Composes with: P11 governs (i)→(ii) at the
    equation level; P12 governs (iii)'s reading of numbers; P15
    enforces the full arc as the unit of authorial competence. The
    failure mode P15 most reliably catches is an opener that lifts
    formal machinery into the body before the reader has a referent
    for what the machinery is FOR -- the prose can pass P11 (figure
    before equation) and P12 (claim-wrapped numbers) yet still leave
    the reader without an applied reading of why the equation was
    introduced.

16. **Theoretical-thread reinforcement (invocation-not-reintroduction)**:
    once a concept has been formally introduced under P15(i) at any
    point in the report, every subsequent reference must invoke it
    without re-introducing. A second mention of `log-ML-as-KL`
    reads `\cref{app:ml-kl}` and uses the term as established
    vocabulary; it does not repeat the derivation, the layman gloss,
    or the citation. Density of cross-references between sections is
    the marker of a well-constructed theoretical thread; conversely,
    repeated re-introductions are the marker of a draft that has not
    yet earned its compression. P16 composes with P9 (invocation by
    reference economy) at the textbook-result scale and with P1
    (layered architecture) at the appendix-cite scale; P16
    generalises both to project-internal concepts -- any concept
    formally introduced in this report becomes invocation-eligible
    for the remainder of the same report. Re-introduction is
    forgivable across distinct documents; within one report it is a
    compression failure. Test: search the report for the concept's
    keyword; the FIRST match should carry the introduction, all
    subsequent matches should be invocations linked by `\cref` or by
    keyword alone.

### Workflow

0. **TOUCH-zone scan** (Principle 13): on the first bash action of
   any LaTeX edit task, run `grep -nE "% TOUCH HERE|% STOP TOUCH"
   <target-file>`. Record every pair. If the planned edit falls
   outside every pair, stop and surface the conflict in chat (see
   Principle 13). If no markers are present, continue with the
   normal workflow below. This step precedes all other reads so that
   the edit surface is known before exploratory reads shape the
   author's expectations for the edit scope.
1. Read the current document structure (chapters, sections,
   subsections).
2. Identify notation reference and build a symbol concordance.
3. For each section: verify content belongs there (separation of
   concerns).
4. For each equation: verify (a) notation matches reference, (b)
   derivation is cited or provided in appendix, (c) all non-
   evident steps are present.
5. For each appendix derivation: verify (a) all intermediate
   steps shown, (b) cited if from external work, (c) audience-
   calibrated thoroughness.
6. Cross-reference: every appendix section is cited from at least
   one main body section; every main body equation that requires
   derivation cites its appendix.
7. For mode-B reports under a page budget, apply sentence-value
   scoring: every sentence must deliver at least one of
   {spec answer, counter-intuitive observation, mechanism,
   reference}. Raw statistics are not self-justifying: a sentence
   containing numbers earns its place only when the numbers are
   embedded in a statement about ordering, deviation, anomaly, or
   threshold crossing (Principle 12). Synthesis paragraphs that
   enumerate per-dataset outcomes should default to a compact table
   or figure caption, not a dense run-on of "dataset A gives X,
   dataset B gives Y" clauses. Reject filler.

## `report-publish` -- Academic LaTeX research report (publishing rigour)

**Use when**: producing LaTeX for external peer review -- conference
or journal submissions, thesis chapters, the ML Reproducibility
Challenge. Audience is specialist reviewers who scrutinise every
claim independently. Distinguished from `report`, which defaults to
the non-publishing posture (Principle 9) appropriate for coursework
and internal assignments.

**Args**: same as `report`
(`<section|full> [--ref <notation-reference.pdf>] [--audience <level>]`).

### Principles

Inherits Principles 1-8, 10-14 of `report` (Principle 9 is suspended;
Principles 10-14 carry over because answer-first posture,
intuition-first prose, stranded-statistic filtering, TOUCH-zone
lockdown, and complexity calibration apply regardless of publication
target -- though Principle 14's "calibration target" shifts from
marker palatability to specialist-reviewer rigour). Amendments below:

- **Principle 9 suspended**: invocation-by-reference economy does NOT
  apply. Every mathematically non-trivial claim the paper's argument
  rests on is derived step-by-step in the main body or appendix, even
  when the result appears in a standard reference. Invocation by name
  (`\cite[\S17.2.1]{murphy2022pml}` for Mercer's theorem, etc.) is
  acceptable only for context sentences that do not carry the
  argument.
- **Literature depth amendment**: every load-bearing foundational
  claim cites BOTH a foundational work (pre-2010 seminal paper for
  the subfield) AND a recent primary or survey paper (prefer
  2023-2026). Single-source citation of an argument-carrying claim
  is a methodology violation (Principle 15, Mode A).
- **Hostile-review gate**: before submission, run `/research
  hostile-review --scope both --mode A` on the draft. Pass-1
  findings are remediated before pass-2 is invoked. A paper that has
  not been through `hostile-review` is not submission-ready (Mode A
  north-star 3).

### Workflow

As `report` workflow (steps 0-7), with these amendments:

- Step 0 (TOUCH-zone scan, Principle 13): retained unchanged from
  `report`. Author-declared lockdown applies to every revision pass
  on a pre-publication draft.
- Step 4 (per-equation audit): verify EVERY non-trivial equation has
  its derivation in the main body or appendix, never invoked by
  reference when the equation carries the argument.
- Step 7 (sentence-value scoring): the page budget is venue-imposed,
  not marker-imposed; budget pressure pushes material to appendices,
  not out of the paper. A truncated derivation under a conference
  page limit is a violation of the Principle 9 suspension and must
  move to the appendix instead.

## `report-technical` -- Technical documentation in Markdown

**Use when**: producing or refining a Markdown technical document
-- reproducibility log, evaluation package README, divergence
catalogue, experimental-matrix README, or any file that renders on
GitHub/GitLab and cross-references source code by line range.

**Args**: `report-technical <section|full> [--target <path>] [--exemplar <path>]`

`--target` is the Markdown file being written or refined;
`--exemplar` optionally points to a reference document that anchors
voice and structure (defaults conceptually to
`docs/reprod-notes.md` and `example-hpc/eval-adm/README.md` from the
example-project reproduction). Optimised for rendering on GitHub/GitLab,
citing source files by line range, and isolating shared
methodology in named subsections rather than academic appendices.

### Principles

1. **Markdown-native structure**: use GitHub-slugged anchors for
   in-file navigation
   (`[§M3](#m3-confidence-intervals-and-the-papers-sem)`), never
   LaTeX constructs (`\label`, `\ref`, `\cite`). Header slugs
   follow GitHub's convention -- lowercase, spaces to hyphens,
   apostrophes and punctuation dropped -- and the table of
   contents is a bulleted list of active links, not a sectioning
   macro.

2. **Section separation of concerns**: each top-level section owns
   one category. A reproducibility log's canonical layout --
   Methodology (shared protocol) / Part A (Successful
   Replications) / Part B (Known Divergences) / Part C (Original
   Bugs and Mistakes) -- places shared protocol, positive results,
   known gaps, and upstream root-cause analyses in distinct,
   non-overlapping containers. Content that fits two sections
   belongs in the more specific one and is cross-referenced from
   the other, never duplicated.

3. **Cite code, do not duplicate**: every claim about code
   behaviour references a file and line range rather than
   reproducing the code in prose. Example from
   `docs/reprod-notes.md §M2`: "see `eval.py:77-167` for the
   sweep implementation (batch construction at `eval.py:57-72`,
   loss and CI computation at `eval.py:135-165`)". The reader
   opens the file to see how; the document explains why.

4. **Active in-file links, plain cross-file references**:
   within-file section references are clickable markdown links
   (`[§B4](#b4-rng-state-restoration-on-ddp-resume-adm--resolved)`);
   cross-file references are plain backticked paths
   (`example-hpc/eval-adm/README.md`, `optim/base.py:218-229`). This
   asymmetry is a DRY requirement: in-file navigation points
   inside the same document, whereas cross-file content must live
   in exactly one place and be linked rather than inlined --
   inlining the target's content into the referrer drifts the
   moment the target changes.

5. **Tables over prose for structured data**: experimental
   matrices, confounder-isolation blocks, result tables, fix
   timelines, and parameter comparisons belong in tables.
   `example-hpc/eval-adm/README.md` presents its 4 x 2 x 2 experiment
   matrix as a six-column table
   (`# | Directory | Method | Config | Outputs | What it isolates`)
   rather than six paragraphs, so a reader can compare rows at a
   glance; prose is reserved for interpretation and narrative.

6. **Quantitative grounding**: every technical claim cites a
   number, a line range, or a file. "The delta is within the
   noise floor" is vague; "the 60k v1 vs v2 delta of 0.014 PPL is
   well below the 0.25 PPL intra-run SEM (see
   `docs/reprod-notes.md §M3`)" is grounded. Uncertainty
   quantities must be named (intra-run cross-batch CI vs
   inter-seed cross-run SEM) and never collapsed.

7. **Named methodological subsections when a concept is
   referenced two or more times**: if a methodological point
   applies to several sections, extract it as its own named
   subsection and cite back to it -- do not flag it as a passing
   caveat. `docs/reprod-notes.md §M3 Confidence intervals and the
   paper's SEM` is the canonical example: the intra-run CI vs
   inter-seed SEM distinction applies to every Part A result
   table, so it lives in a four-paragraph named subsection and
   every A-row cites `(see [§M3](#m3-...))`. An earlier draft
   merely flagged the distinction as a sentence-long caveat; a
   reviewer caught that it had to be a named subsection every
   reader reaches before interpreting any row.

8. **Lexical and rendering hygiene**: per Cross-subcommand
   conventions (UK English, no emoji, no ASCII emoticon
   substitution triggers). Every ToC slug must match its header
   exactly: run a GitHub-slug emulator, or grep each in-file link
   against the corresponding header, before committing.

### Workflow

1. Read the target Markdown file end-to-end. If the target does
   not exist yet, read the exemplar (default:
   `docs/reprod-notes.md` and `example-hpc/eval-adm/README.md` from
   the example-project reproduction) to anchor voice, tone, and
   structural conventions.
2. Separation-of-concerns audit: for each section, verify its
   content belongs there. Grep for concept keywords across
   sections (`CI`, `SEM`, `delta`, `resume`, `cascade`); any
   concept appearing with divergent framings in two or more
   sections must become a named methodological subsection rather
   than a scattered caveat.
3. Technical-claim audit: for each factual statement about code
   or data, verify (a) a file citation with line range or a
   numeric grounding (not "the code does X" but
   "`optim/base.py:64-79` restores per-rank RNG state"), (b)
   in-file section references are active markdown links using
   GitHub-slugged anchors, (c) ToC entries match their section
   headers character-for-character.
4. Link audit: every `](#anchor)` is reachable, every header
   slug matches its link target. Use `grep -nE '\]\(#[^)]+\)'` to
   enumerate in-file links, then verify each resolves to a real
   header. A GitHub-slug emulator is stricter than a human eye
   -- always check punctuation drops and apostrophe handling.
5. Lexical audit: grep the file for the two-character ASCII
   emoticon sequences (heart, smile, grin) that GitHub's
   Markdown renderer substitutes into emoji glyphs, and require
   zero hits. Replace any substitution trap with an explicit
   equivalent -- write "less than 3 percent" in words, or insert
   a space between the less-than sign and the digit.
6. DRY audit: every cross-file reference is a plain backticked
   path, not an inlined duplication of the target file's content.
   If the referrer paraphrases the target, delete the paraphrase
   and cite the path -- the target is the single source of truth.

## Constraints

- File-based analysis only -- never import/run ML code under
  study; reading data files is fine.
- All paths absolute, respect git restrictions, respect project
  `/push` policy.
- Every recommendation must cite evidence (file + line, or paper
  + section).
- Never recommend an experiment without stating what truth it
  seeks.
- Ultrathink + /effort max mandatory for `hostile-review`;
  strongly recommended for `design`, `literature`, `gap-audit`.
  Abort rather than proceed at lower effort on these.
