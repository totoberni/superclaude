---
name: research
description: "Use when running scientific research, design, literature, review, report"
category: domain
user-invocable: true
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

The 15 shared Principles (quality bar for every subcommand) live in
[references/principles.md](references/principles.md); cited by number (Principle 1-15, 3a).

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

## Subcommands

| Subcommand | Reference | Synopsis |
|------------|-----------|----------|
| `design` | [references/design.md](references/design.md) | Design a mechanistic investigation |
| `paper` | [references/paper.md](references/paper.md) | Find implementation of a paper |
| `literature` | [references/literature.md](references/literature.md) | Systematic literature survey |
| `ablation` | [references/ablation.md](references/ablation.md) | Design ablation study |
| `hyperparams` | [references/hyperparams.md](references/hyperparams.md) | Compare against paper |
| `reproduce` | [references/reproduce.md](references/reproduce.md) | Check reproduction fidelity |
| `gap-audit` | [references/gap-audit.md](references/gap-audit.md) | Conceptual-vs-technical divergence diagnostic |
| `hostile-review` | [../hostile-review/SKILL.md](../hostile-review/SKILL.md) | Adversarial second-reviewer pass (standalone skill) |
| `report` | [references/report.md](references/report.md) | Academic LaTeX report (non-publishing default) |
| `report-publish` | [references/report.md](references/report.md) | Academic LaTeX report (publishing rigour) |
| `report-technical` | [references/report.md](references/report.md) | Technical documentation in Markdown |

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
