---
name: report
description: "Use when writing a branded LaTeX PDF report: executive default or --scientific"
category: domain
user-invocable: true
argument-hint: "<slug> [--scientific] [--lang it|en] [--brand <name>] [--from <analysis-dir>]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Skill
---

# Report

Produces a branded PDF report from a LaTeX source, in the look of the Nomisma
methodology note: maroon caps running header, navy bold title, numbered navy
headings with a thin maroon rule, sans body (Segoe UI), navy-header tables with
light grey alternating rows, grey right-aligned footer "<brand> | <project> · pag. N".
Two audience presets: **executive** (default) and **scientific**.

The `.tex` is the only source. The PDF is what gets circulated.

## Methodology is sacred

Standing owner directive (2026-10-12). The methodology is the most thorough part
of every report, in both modes: it is the visible proof that the work was done and
it lets others understand exactly WHAT was done. Be more complete there than
anywhere else, but never add detail the reader cannot follow: plain language, no
unexplained jargon.

- Every rule and threshold is stated with its reason.
- Every data source is stated with its date and coverage.
- Every judgement call is stated with the alternatives considered and what each would have produced.
- Every known limit is stated plainly, with a concrete example.
- Be most explicit where a choice looks odd or asymmetric. Example: a score threshold that differs between sectors to reach a size target. The report says what the threshold is, why it differs, what the alternatives were and what they would have produced.

## Which skill

| Need | Skill |
|---|---|
| Client wants an editable Word file; Quarto source with analysis chunks | `/deliverable-it` |
| Coursework, thesis chapter, paper; method design, literature, peer-review rigour | `/research report` or `report-publish` |
| Branded PDF to circulate: executive summary for busy readers, or a technical report in house look | `/report` |

One source per deliverable: do not use `/report` to make the PDF twin of a
`/deliverable-it` Word file. Scientific method, claim provenance and statistics
standards belong to `/research`; this skill points to them instead of restating them.

## Invocation

`/report <slug> [--scientific] [--lang it|en] [--brand <name>] [--from <analysis-dir>]`

- `<slug>`: source stem, ASCII (`ExcITA_report`). The PDF gets a reader-facing name (`ExcITA_Made_in_Italy_report.pdf`).
- `--scientific`: technical report for mathematics, ML analysis, statistical models. Default is executive.
- `--lang`: `it` (default, client deliverables) or `en` (UK English, per `/research` rendering hygiene). Sets `\ReportLang` (`italian`/`english`): hyphenation, number format, captions, "pag." vs "p.".
- `--brand`: default `Nomisma`. Sets `\ReportBrand`; `\ReportProject` adds the second header part (`NOMISMA | LEONARDO NOMISMA INDEX`). The palette is Nomisma's; for another brand ask the owner for the hex colours and change only the six `\definecolor` lines.
- `--from`: directory with the analysis outputs (tables, parquet/csv, figures, READMEs). Default: the current project.

## Templates

| File | Role |
|---|---|
| `templates/report-base.tex` | Shared preamble: fonts, colours, header/footer, `\maketitleblock`, section style, `riquadro` box, `tabella` table style, `\num`/`\pct` number format, hyperref. Copied next to the source and loaded with `\input{report-base}`. |
| `templates/skeleton-exec.tex` | Executive scaffold, config block on top, one `% GUIDA:` comment per section. |
| `templates/skeleton-scientific.tex` | Scientific scaffold: numbered subsections, biblatex (biber, author-year), cleveref, equation, captioned tables, figure. |
| `templates/references.bib` | Minimal BibTeX example for scientific mode. |
| `references/style-exec.md` | Executive writing guidance with good/bad Italian examples. |
| `references/style-scientific.md` | Scientific writing guidance: metric with CI, equation block, ablation table. |

Template conventions: placeholders are `<<...>>`; guidance comments start with `% GUIDA:`; template comments are Italian (rule 07). Table header row: `\rowcolor{navy}\hd{...}`; long tables use `xltabular` with the header before `\endhead` so it repeats on page breaks. Captioned `xltabular` rows start with `\rowcolor{white}\caption{...}`.

## Modes

### Executive (default)

Readers are busy non-specialists. Typical length 4 to 6 pages.

- Plain human language, short paragraphs, every technical idea explained in everyday words (an index is followed by what it means and what a good value is).
- No code, variable names or rule codes in prose. Codes appear only inside tables.
- A boxed "In sintesi" (`riquadro`) with the headline numbers, right after a two or three sentence opening.
- Do not understate the work: state scale and effort factually (volumes, sources crossed, cases checked one by one, alternatives evaluated and rejected). Confident but factual; no superlative without a number behind it.
- Every number computed from the data and logged with its source (procedure step 1).
- Where AI-assisted tools were used, one honest sentence on how, and where each judgement is recorded.
- "Limiti e come leggere" gives each limit its size (how many units, what share) and how to read around it.
- "Materiali consegnati" lists the files; any code package is mentioned in passing and points to its README. Setup and usage are out of scope.
- Author name, role and date under the title.

### Scientific (`--scientific`)

For mathematics, ML analysis, statistical models. Length driven by content.

- Problem formalisation and notation (each symbol defined once, used consistently), model specification with numbered equations, data and preprocessing, estimation or training details, evaluation design (splits, baselines, metrics with uncertainty: CIs or multiple seeds), ablations and robustness, diagnostics, threats to validity, reproducibility (code version, library versions, seeds, data provenance), captioned figures, BibTeX references.
- Inherits the `/research` Principles: `~/.claude/skills/research/references/principles.md` (claim provenance, triangulation, falsifiability, verdict-language calibration, multi-seed where applicable). LaTeX conventions (layered information, appendix derivations, `\cref`, notational coherence): `~/.claude/skills/research/references/report.md`. Multi-seed runs and claim provenance: `/experiment-harness`. Figures: `/figure-validate`.

## Section skeleton

Standing owner directive (2026-10-12). Both modes follow one order: **what**
(inception: what was asked, what the object is), **why** (relevance), **how**
(methodology, the most thorough part), **what was found** (results),
**interpretation and limits**, **changelog** (revised editions only). The tables
below are that order; adapt the names, not the sequence.

| Executive (it) | (en) | Order | Content |
|---|---|---|---|
| In sintesi | Summary | front | Opening sentences on what, scale, effort; box with 3 to 5 headline numbers; `\tableofcontents` right after |
| Il punto di partenza | Starting point | what | The question, the object, the definition adopted, what the result had to guarantee |
| Perché conta | Why it matters | why | Relevance for the reader and the decision it supports (may be folded into the previous section when short) |
| Come funziona il metodo | How the method works | how | One `\subsection` per block: data sources (table `Fonte \| Che cosa contiene \| Perché la usiamo \| Copertura e note`, alternatives rejected with the reason), numbered plain steps (rule codes only in a table), thresholds and calibration, checks and edge cases (cross-checks, individual reviews, AI-assistance sentence) |
| Risultati | Results | found | Table first, then a two or three sentence reading with the most informative comparison |
| Limiti e come leggere i risultati | Limitations and how to read the results | interpretation | Each limit with its size |
| Materiali consegnati | Delivered materials | back | Files; code package in passing, README pointer |
| Note di versione | Version notes | changelog | Revised editions only, always last; see "Versions are self-contained" |

| Scientific (it) | (en) | Order | Content |
|---|---|---|---|
| Abstract | Abstract | front | Problem, method, data, main result with uncertainty, main limit; 150 to 250 words; `\tableofcontents` right after |
| Introduzione | Introduction | what, why | Research question and object, why it matters, contributions, essential literature |
| Dati | Data | how | Provenance, period, units, inclusion criteria, preprocessing, splits |
| Metodo | Method | how | Formalisation, notation, model, estimation/training |
| Esperimenti e valutazione | Experiments and evaluation | how | Splits, baselines, metrics, seeds, ablations, robustness |
| Risultati | Results | found | Every number with its uncertainty and its table/figure |
| Discussione e limiti | Discussion and limitations | interpretation | What is and is not shown; threats to validity |
| Riproducibilità | Reproducibility | back | Code version, environment, seeds, data provenance, rerun command |
| Bibliografia | References | back | `\printbibliography` |
| Note di versione | Version notes | changelog | Revised editions only, always last |

### Structure rules (both modes)

- **Versions are self-contained.** A revised edition serves the same purpose as the previous one and reads as if no earlier version existed: executives need what was found, not the story of what changed. Every comparison with a previous version, old threshold, old number and "what changed" narrative goes into the single final "Note di versione" section. Timeless method facts stay in the method.
- **Subsections.** Use `\subsection{}` whenever a section discusses more than 2 items, especially the methodology: one per pillar or criterion, data sources, rules, thresholds and calibration, verification.
- **Table of contents.** Always include `\tableofcontents` after the title block (and the executive summary).
- **Tables.** Put each table in a float with `\caption` and `\label`, refer to it with `\cref`/`\ref`, and use `\FloatBarrier` (placeins) at section ends so floats never drift into the next section or split awkwardly. `\resizebox` only as a last resort; genuinely long tables use `xltabular` with a repeated header.

The methodology section ("Come funziona il metodo" / "Metodo" with "Dati") is the
most thorough section of the report. It contains: sources with date and coverage;
each step or rule in execution order, with its reason; parameters with their values
and how they were chosen; judgement calls with the alternatives considered and
their effect; known limits with a concrete example; the validation done. Executive
mode keeps it readable by non-specialists (tables and plain sentences) and moves
technical detail to an appendix rather than cutting it.

## Procedure

### 1. Collect inputs and compute every number

Inventory the analysis outputs under `--from` (tables, figures, READMEs, source
documents). Then write a numbers script (Python, in the project's analysis code,
not in the deliverables folder) that recomputes from the data every number the
report will cite: counts, shares, metrics, coverage. Log a numbers checklist:

| # | Number as written | Computed value | Source (file, filter, file mtime) | Section |
|---|---|---|---|---|

The checklist lives in the worker's checkpoint (or the hand-off report), never
in the deliverables. A number with no traceable producer is a blocker: stop and
ask. If a brief gives a number that differs from the data, the data wins and the
discrepancy is reported to the caller.

### 2. Write

Copy `report-base.tex` and the mode skeleton into `<deliverables>/report/`,
rename the skeleton to `<slug>.tex`, fill the config block (title with the scale,
subtitle, author, role, date, brand, project), then write section by section
following `references/style-exec.md` or `references/style-scientific.md`.
Executive: write "In sintesi" last, from the finished sections. Scientific:
copy `references.bib` and keep only entries actually cited.

### 3. Compile

Run from the workspace with absolute paths (no `cd`):

```bash
X="C:/Users/berni.alberto/AppData/Local/Programs/MiKTeX/miktex/bin/x64"
R="<abs-project>/deliverables/report"
"$X/xelatex.exe" -interaction=nonstopmode -halt-on-error -output-directory="$R" --include-directory="$R" "$R/<slug>.tex"
# solo scientifico: "$X/biber.exe" --input-directory "$R" --output-directory "$R" <slug>
"$X/xelatex.exe" -interaction=nonstopmode -halt-on-error -output-directory="$R" --include-directory="$R" "$R/<slug>.tex"
# terzo passaggio xelatex in entrambe le modalità: indice e riferimenti alle tabelle si stabilizzano solo al terzo
"$X/xelatex.exe" -interaction=nonstopmode -halt-on-error -output-directory="$R" --include-directory="$R" "$R/<slug>.tex"
```

`--include-directory` lets `\input{report-base}`, `references.bib` and
`figures/...` resolve relative to the report folder.

### 4. Verify (mandatory before reporting done)

Run the checklist below. Fix and recompile until every line passes.

### 5. Deliver

Move the PDF next to the other deliverables (`<deliverables>/<Reader_Facing_Name>.pdf`).
Keep in `<deliverables>/report/` only `<slug>.tex`, `report-base.tex` and assets
(`figures/`, `references.bib`). Delete build artefacts by name:
`.aux .log .out .bbl .bcf .blg .run.xml .toc` and the in-folder `.pdf`.
Before writing to a project folder, check the draft against the codebase
firewall (`~/.claude/rules/20-tool-conventions.md`): no superclaude paths or IDs.

## Verification checklist

```
Report: <abs path>.pdf (<n> pages)
  compile             PASS|FAIL  xelatex x3 (+biber in scientific), exit 0, no "??" refs
  log clean           PASS|FAIL  errors=0 Overfull=0 Underfull=0 undefined refs/citations=0
  visual pages        PASS|FAIL  all <n> pages read; style matches reference
  table flow          PASS|FAIL  no half-empty page, headers repeat, no orphan heading
  structure order     PASS|FAIL  what, why, how, found, interpretation/limits, changelog (if revised)
  toc                 PASS|FAIL  \tableofcontents present after title block / summary
  subsections         PASS|FAIL  every section with more than 2 items has \subsection (methodology above all)
  self-contained      PASS|FAIL  previous-version mentions outside "Note di versione" = 0
  tables placed       PASS|FAIL  float + \caption + \label + \cref; \FloatBarrier; no split table
  dashes              PASS|FAIL  U+2014=0 U+2013=0 "--"=0 in .tex
  placeholders        PASS|FAIL  "<<"=0, "% GUIDA"=0, no all-zero \num placeholders
  italian             PASS|FAIL  accents correct (perché, è, È, più, città)
  numbers             PASS|FAIL  <n> numbers reconciled, <n> discrepancies reported
  jargon (exec)       PASS|FAIL|N/A  no variable names or rule codes outside tables
  references (sci)    PASS|FAIL|N/A  every \cite resolves; no invented entries
  methodology         PASS|FAIL  thresholds and choices explained; non-specialist can restate the method
```

How to check:

- **Log**: `grep -c -E "^! |Overfull|Underfull" <slug>.log` must be 0; also `grep -E "undefined|Citation" <slug>.log`.
- **Visual**: Read the PDF with the `pages` parameter, every page. Compare page 1 with the style reference. Look for: a table pushed to the next page leaving a half-empty page (switch `tabularx` to `xltabular`, never wrap a long table in a float); a heading alone at the bottom (`\needspace{8\baselineskip}` before it; sections already reserve 6 lines); header and footer on every page.
- **Structure**: list `\section`, `\subsection` and `\tableofcontents` in order (`grep -n -E "\\\\(section|subsection|tableofcontents)" <slug>.tex`) and compare with the skeleton order; the changelog, if present, is the last numbered section.
- **Self-contained**: outside "Note di versione", `grep -n -i -E "prima versione|versione precedente|\bv[0-9]\b|questa versione|previous version|earlier version|this version" <slug>.tex` must return 0 hits (English equivalents in `--lang en`); old thresholds and numbers count too.
- **Tables**: every table sits in a float with `\caption` and `\label` and is cited via `\cref`/`\ref`; `\FloatBarrier` closes sections with floats; a table that still splits is `xltabular` with `\endhead`; `\resizebox` only if nothing else fits.
- **Dashes**: count with Python, since `grep -P` needs a UTF-8 locale: `python -c "import sys;t=open(sys.argv[1],encoding='utf-8').read();print(t.count(chr(0x2014)),t.count(chr(0x2013)),t.count('--'))" <slug>.tex` must print `0 0 0`. TeX turns `--` and `---` into en and em dashes; ranges use one hyphen (`2016-2026`). biblatex page ranges are already set to a hyphen in the scientific skeleton.
- **Placeholders**: `grep -c "<<" <slug>.tex` and `grep -c "% GUIDA" <slug>.tex` both 0. Skeleton numeric slots are all-zero (`\num{0}`, `\num{0.000 +- 0.000}`): none may survive unless the true value is zero.
- **Italian accents**: grave on è, cioè, più, città, perciò; acute on perché, poiché, né, sé; capital È typed as the character, never `E'`.
- **Methodology**: (a) every threshold, cut-off and parameter appearing in the results is in the methodology with its reason; (b) every asymmetric or surprising choice (e.g. a threshold that differs between groups) has its rationale and the alternatives with their effect; (c) a non-specialist reading only the methodology section can restate what was done, from which sources, and with which known limits.
- **Jargon sweep (exec)**: outside `tabella` blocks, no `\_` identifiers, no `\texttt` except in "Materiali consegnati", no rule-code tokens (pattern `\b[A-Z]{1,3}[0-9]+[a-z]?\b`).

## Toolchain notes

- Windows + MiKTeX (user scope), binaries in `C:\Users\berni.alberto\AppData\Local\Programs\MiKTeX\miktex\bin\x64\` (`xelatex.exe`, `biber.exe`, `kpsewhich.exe`, `initexmf.exe`). Engine: xelatex with fontspec.
- Font: Segoe UI from `C:/Windows/Fonts/segoeui*.ttf` (regular, `b`, `i`, `z`), as in the exemplar. Off Windows the preamble falls back to Latin Modern Sans. `\XeTeXgenerateactualtext=1` keeps PDF text extraction correct (Segoe UI parentheses otherwise copy as U+FD3E/U+FD3F).
- Packages used: fontspec, babel (italian, english), geometry, xcolor[table], colortbl, array, tabularx, xltabular, titlesec, fancyhdr, enumitem, microtype, needspace, titletoc, placeins, amsmath, amssymb, graphicx, caption, siunitx, etoolbox, hyperref, cleveref (both modes, loaded after hyperref); scientific adds csquotes, biblatex. MiKTeX installs missing packages on the fly when AutoInstall is on (`initexmf --show-config-value=[MPM]AutoInstall` prints 1); otherwise `miktex packages install <name>` and rerun. A recurring "you have not checked for MiKTeX updates" notice clears with `initexmf --report`.
- Numbers: write raw values in `\num{161250}`, `\num{0.698}`, `\pct{96.6}`, `\num{0.874 +- 0.006}`; output follows `\ReportLang` (161.250 / 0,698 / 96,6% in Italian). Literal Italian numbers (`161.250`, `96,6\%`) are also fine in prose; never mix decimal styles in one table.
- Escaping: `\%`, `\&`, `\_`, `\#`, `\$`, `\{ \}`, `\textasciitilde{}`, `\textbackslash{}`. Accented letters are typed directly (UTF-8 source). Straight apostrophes render curly. Quotes: “ ” typed, or `\enquote{}` in scientific mode (csquotes gives «» in Italian).
- `\section*` (numberless) uses the same navy-and-maroon style without number (Abstract, Bibliografia).

## Delegation (for meta)

- Authoring is a `w-doc` task: opus for a multi-section report (both modes), sonnet only for a single-section polish.
- Numbers extraction can be a separate `w-implementer` (numbers script plus checklist), dispatched first; the writer consumes its checklist (rule R-1: producer first).
- Review before hand-off: `w-reviewer` with an Italian-register rubric (plain language, accents, jargon sweep, numbers vs checklist). For `--scientific` also `w-hostile-reviewer` with the `hostile-review` rubric.
- The reviewer reruns the compile and reads the PDF pages itself; a producer's self-report is not evidence.

## Boundaries

- Never invent a number, a source, a reference or a date. Missing input is a blocker.
- Never commit; the owner commits.
- Client data stays out of any prompt sent beyond this machine.
- No em dash or en dash anywhere (rule 06), including the PDF output.

## Cross-references

`templates/`, `references/style-exec.md`, `references/style-scientific.md`,
`~/.claude/skills/deliverable-it/SKILL.md` (Word route; Italian prose patterns in
`deliverable-it/references/patterns-it.md`), `~/.claude/skills/research/SKILL.md`,
`~/.claude/rules/06-writing-style.md`, `~/.claude/rules/07-language.md`.
