> Part of /research (see ../SKILL.md). Subcommands: report, report-publish, report-technical (the output-format family).

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
