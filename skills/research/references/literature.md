> Part of /research (see ../SKILL.md). Subcommand: literature.

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
