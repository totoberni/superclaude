---
name: model-review
description: "Use when asked to review this model, check the fit, hunt for leakage, or sanity-check metrics before reporting them."
category: meta
user-invocable: true
argument-hint: "<path-to-notebook|script|R-file> [--claims \"<the metric numbers being reported>\"] [--lang py|r]"
allowed-tools: Read, Bash, Grep, Glob
---

## `model-review`: leakage and validity gate for a fitted predictive model

**Use when** a regression, SVM, tree ensemble (XGBoost, LightGBM, random forest)
or forecasting model has been fitted and its numbers are about to leave the
notebook: a client deck, an internal memo, a model hand-off. Triggers include
"review this model", "check the fit", "is there leakage here", and
"before I report these metrics".

This is a reviewer rubric. It is run by a reviewer subagent (`w-reviewer`) or by
the owner directly, it returns a `VERDICT:` token on line 1, and it is
single-pass: one dispatch, one verdict (`_shared/verdict-schema.md`).

### Inputs

| Input | Required | Notes |
|---|---|---|
| Artefact | yes | one `.ipynb`, `.py`, `.R`, `.Rmd` or `.qmd`, or a small directory of them |
| Metric claims | yes | the exact numbers about to be reported (RMSE 0.42, AUC 0.91, MAPE 3.1%) and the sentence they support |
| Data dictionary | no | if present, it is the authority on feature availability at prediction time |
| `--lang` | no | inferred from the extension; `r` selects the R branch of the rubric |

Without the metric claims the review cannot be run: half of the rubric asks
whether a specific number is reproducible and whether it answers the business
question. Ask for them rather than guessing.

### Procedure

Read `references/rubric.md` first; it carries the checks and their severities.
Then locate each of the six anchors below in the artefact and record a
`file:line` for each. An anchor you cannot find is itself a finding (an absent
split is `blocking`; an absent seed is `minor`).

1. **The split.** Where are train and test separated? Which line, which
   function, which fraction? Is it a random split on data that is grouped or
   ordered in time? Note whether anything happens to the data above that line.
2. **The preprocessing.** Every scaler, encoder, imputer, feature selector,
   PCA, target encoder and resampler: is each fitted on training rows only, or
   on the full frame? Record the fit call site and the data it receives.
3. **The target construction.** How is `y` built? From which columns, with
   which shift or aggregation? Any column used to build `y` that survives into
   `X`, or any proxy of it, is leakage.
4. **The cross-validation scheme.** Which splitter, which folds, shuffled or
   not, grouped or not, expanding or not? Is the whole preprocessing chain
   inside the fold (a pipeline passed to the CV call) or outside it?
5. **The metric computation.** Which metric, on which rows, against which
   baseline? Is the reported number the test score or the best CV score after
   tuning (an optimistically biased choice)?
6. **The reproducibility surface.** Seeds, library versions, the data snapshot
   the fit consumed, and whether a clean re-run reproduces the claimed number.

Where a claim can be checked cheaply, check it: re-run the metric cell, count
duplicate rows, print the split sizes. A finding backed by a re-run beats a
finding backed by reading.

### Severity and the blocking rule

Severities follow `_shared/verdict-schema.md` (`blocking`, `major`, `minor`).
Two classes are always `blocking`:

- **Any leakage.** Any of the eight leakage types in `references/rubric.md`
  section 1, 2 or 3 that is present in the code, whatever its estimated size.
  A leak makes the reported number a measurement of the leak, not of the model.
- **Any unreproducible metric.** A claimed number that a clean re-run does not
  return, or that no cell in the artefact actually computes.

`major` covers checks that would make a competent reader push back: a metric
that does not match the decision being supported, resampling placed outside the
fold, an uncontrolled seed on a claim quoted to three decimals. `minor` covers
polish: unpinned versions, a missing baseline comparison, undocumented feature
provenance where the feature is plainly legitimate.

`VERDICT: CLEAN` requires `blocking=0 major=0`. Findings without a `file:line`
(or a re-run with expected against actual) are dropped before counting; a
zero-finding review is a valid outcome.

### Output

Line 1 of the return, verbatim:

```
VERDICT: REWORK|CLEAN blocking=N major=N minor=N
```

Then the findings table, sorted by severity then by fix effort:

```
| # | severity | finding | file:line | fix |
|---|---|---|---|---|
| 1 | blocking | Scaler fitted on the full frame before the split, so test statistics enter training | model.ipynb:cell 7 line 3 | Move StandardScaler into a Pipeline and fit inside the CV fold |
```

Then, in at most five lines: which of the six anchors were found, which metric
claims were re-run and whether they reproduced, and any check the rubric asks
for that the artefact did not give enough information to run (state it as
"not determinable", never as a pass).

### Notes

- The R branch (tidymodels, caret, base R) is section 9 of the rubric. Run the
  same nine sections; only the code symptoms change.
- Never edit the artefact. This skill reviews; the fix wave is someone else's
  dispatch.
- A model that is honest and mediocre is a `CLEAN` review. Do not manufacture
  findings to justify the pass.

### Cross-references

- Rubric: `references/rubric.md`
- Verdict tokens, severity map, no pre-approval: `../_shared/verdict-schema.md`
- Code-level review of the same file: `../code-quality/SKILL.md`
