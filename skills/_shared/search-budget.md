# _shared/search-budget.md: web-search budget discipline (SOT)

Consumed by: /research (subcommands requiring literature work). Reusable by any skill or worker performing web/literature search.

## Rule

Accept a `--web-search-budget <N>` parameter (default 10-15 searches). Do not exceed the budget once set.

## Prioritisation (by expected information gain)

1. Novelty claims and threshold/provenance questions first -- the things a wrong answer would most damage.
2. Reference implementations second.
3. Wildcard/exploratory reserves last -- spend these only if budget remains after 1-2.

## Per-search discipline

- Cite URL + title + a one-line takeaway for every search performed. No uncited claims.
- Time-box each search at 2-3 minutes. A query that is not converging gets logged as inconclusive; move to the next rather than deepening.

## Cross-reference

Web-search results still fall under general evidence rules: a claim without a citable source (URL/DOI/arxiv ID) is dropped, not downgraded. Used within [discovery-protocol.md](discovery-protocol.md)'s spec/product-under-review steps whenever literature work is in scope.
