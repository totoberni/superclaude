---
name: deliverable-it
description: "Use when building an Italian deliverable: report per il cliente, relazione, market intelligence report"
category: domain
user-invocable: true
argument-hint: "<slug> [--from <analysis-dir>] [--format docx|pdf|both] [--no-quarto]"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Skill
---

# Deliverable IT

Assembles an Italian client or market intelligence report from analysis outputs
(tables, figures, notebook results) into a single Quarto source that renders to
Word and PDF. One `.qmd` is the source of truth; Word is what the client edits,
PDF is what gets circulated.

Scope: real estate, supply chains, public administration, finance, media. The
deliverable text is Italian (rule 07); identifiers, file names and chunk labels
stay English.

## Invocation

`/deliverable-it <slug> [--from <analysis-dir>] [--format docx|pdf|both] [--no-quarto]`

- `<slug>`: output stem, kebab-case ASCII (`mercato-lavoro-piemonte-2026`).
- `--from`: directory holding the analysis outputs. Default: the current project.
- `--format`: default `both`.
- `--no-quarto`: force the fallback path below even if Quarto is on PATH.

## Procedure

### 1. Collect the inputs

Inventory before writing a single line of prose. Record each item with its path:

- Tables: `.csv`, `.parquet`, `.xlsx` produced by the analysis.
- Figures: `.png`, `.pdf`, `.svg`. Note the producing script or notebook cell.
- Notebook outputs: read the `.ipynb` with the Read tool; never edit it here.
  For any notebook mutation or re-execution, invoke the `notebook` skill first.
- Sources: dataset provenance, extraction date, licence or contract constraints.

Stop and ask the owner if a number in the draft has no traceable producer. An
untraceable figure is a blocker, not a rounding question.

### 2. Draft the structure

Fixed skeleton; Italian headings, no title case (see `references/patterns-it.md`):

| Section | Italian heading | Content |
|---|---|---|
| Executive summary | `Sintesi` | 200 to 400 words, the three findings that change a decision |
| Context | `Contesto e perimetro` | market, time window, geography, what is excluded |
| Method | `Metodo e fonti` | data sources, extraction date, model or estimator, known limits |
| Findings | `Evidenze` | one subsection per finding, each anchored to a table or figure |
| Implications | `Implicazioni` | what the client should do or watch, with the residual uncertainty |
| Appendix | `Appendice` | full tables, methodological detail, glossary |

Write `Sintesi` last, from the finished `Evidenze`.

### 3. Write the Italian body

Copy `templates/report.qmd` and `templates/_quarto.yml` into the deliverable
directory, set the metadata, then write the prose. Non negotiable:

- Rule 07: the prose is Italian; chunk comments are Italian; identifiers,
  labels (`fig-`, `tbl-`), file names and column names stay English.
- Rule 06: no em dash and no en dash anywhere, including ranges. A year range is
  `2019-2024` with a plain hyphen.
- Numbers: comma decimal, thin space thousands, `mln` and `mld`, `%` attached to
  the digits. The detail is in `references/patterns-it.md`.

### 4. Run the prose check

Read `references/patterns-it.md` and pass the draft against every pattern. For
each hit: rewrite in place, or record why the pattern is a false positive in
this context. Report the count of hits fixed. This step is not optional; it is
the difference between a deliverable and a draft that reads machine made.

### 5. Verify the figures

Invoke `figure-validate` on every figure the report embeds:

```
/figure-validate <fig1.png> <fig2.png> --mode image
```

Any flagged figure is regenerated at the source, never patched in the report.

### 6. Render

```
quarto render <slug>.qmd --to docx
quarto render <slug>.qmd --to pdf
```

PDF goes through xelatex (MiKTeX, user scope). Install route, Italian package
needs and the `reference-doc` procedure are in `references/quarto-notes.md`.
First render on a new machine: run `quarto check` first.

### 7. Report

Return the absolute paths of the rendered files plus the checklist result:

```
Rendered:
  <abs path>.docx
  <abs path>.pdf
Checklist:
  inputs traced        PASS|FAIL (<n> items, <n> untraceable)
  structure complete   PASS|FAIL (missing: <sections>)
  rule 06 dashes       PASS|FAIL (<n> fixed)
  rule 07 language     PASS|FAIL
  patterns-it          PASS|FAIL (<n> hits fixed, <n> accepted)
  figure-validate      PASS|FAIL (<n> flags)
  render docx          PASS|FAIL
  render pdf           PASS|FAIL
```

## Fallback without Quarto

Quarto may not be installed (owner decision; see `references/quarto-notes.md`).
The skill still runs:

1. Write the `.qmd` anyway, complete, with the chunks and the YAML header. It is
   the source of truth and it renders the day Quarto lands.
2. Execute the analysis chunks yourself (the `notebook` skill, or a plain script
   run) and paste the resulting tables and figure references into a Markdown
   body extracted from the `.qmd`: strip the YAML header, replace each chunk
   with its rendered table or an image reference.
3. Produce the Word file from that Markdown body with the installed
   `anthropic-skills:docx` skill.
4. PDF: skip it, or state in the report that PDF awaits Quarto. Do not hand roll
   a LaTeX preamble to fake the PDF path; a second, divergent source is worse
   than one missing output.
5. The checklist still runs; mark `render docx FALLBACK` and `render pdf SKIP`.

## Boundaries

- Never invent a number, a source or a date. Missing input is a blocker.
- Never commit; the owner commits.
- Client data stays out of any prompt sent beyond this machine.
- The `reference-doc` is the client's house style. If the placeholder is absent,
  render without it and say so; do not substitute a different template.

## Cross-references

`references/patterns-it.md` (prose patterns), `references/quarto-notes.md`
(Quarto and MiKTeX setup), `templates/report.qmd`, `templates/_quarto.yml`,
`~/.claude/rules/06-writing-style.md`, `~/.claude/rules/07-language.md`.
