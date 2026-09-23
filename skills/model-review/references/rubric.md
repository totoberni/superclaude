# model-review rubric

Nine sections. Each row is one check: what to look for, how it shows up in
code, and the severity it carries when it is present. Severities map onto
`blocking` / `major` / `minor` per `../_shared/verdict-schema.md`.

The checks are paraphrased from three primary sources. Nothing below is quoted
from them; consult them when a judgement call is close.

- Kapoor and Narayanan, *Leakage and the Reproducibility Crisis in ML-based
  Science*, arXiv:2207.07048 (<https://arxiv.org/abs/2207.07048>), for the
  eight-type leakage taxonomy and the model info sheet
  (<https://reproducible.cs.princeton.edu/>).
- scikit-learn, *Common pitfalls and recommended practices*
  (<https://scikit-learn.org/stable/common_pitfalls.html>), for
  fit-on-train-only, pipelines, and randomness control.
- imbalanced-learn, *Common pitfalls and recommended practices*
  (<https://imbalanced-learn.org/stable/common_pitfalls.html>), for resampling
  placement.

The leakage taxonomy is the spine of sections 1 to 3. Its three families are:
clean train and test separation (L1.1 no test set, L1.2 preprocessing across
train and test, L1.3 feature selection across train and test, L1.4 duplicates),
illegitimate features (L2), and a test set not drawn from the distribution the
claim is about (L3.1 temporal leakage, L3.2 non-independence between train and
test rows, L3.3 sampling bias in the test distribution). The model info sheet
turns these into three arguments the analyst owes the reader: why the test set
never touched training, why every feature is legitimate, and why the test set
represents the population the claim is about. A review that cannot reconstruct
those three arguments from the code has found something.

---

## 1. Target leakage and preprocessing leakage

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 1.1 | A test set exists at all (L1.1) | Performance quoted on the rows the model was fitted on | `model.fit(X, y)` then `score(X, y)`; a single `predict` on the full frame; training accuracy reported as the headline | blocking |
| 1.2 | Preprocessing fitted on training rows only (L1.2) | Scalers, encoders, PCA and imputers learning their statistics from all rows | `scaler.fit_transform(X)` before `train_test_split`; `pd.get_dummies(full_df)`; `df.fillna(df.mean())` on the full frame | blocking |
| 1.3 | Feature selection fitted on training rows only (L1.3) | Any ranking, filtering or importance step that saw the test labels | `SelectKBest(...).fit_transform(X, y)` above the split; a correlation-with-target screen on the full frame; features hand-picked after inspecting a full-data correlation matrix | blocking |
| 1.4 | Duplicate and near-duplicate rows (L1.4) | The same record, or the same entity at near-identical timestamps, on both sides of the split | no `drop_duplicates` before splitting; a merge that fanned out rows; panel data split row-wise | blocking |
| 1.5 | Target built from a column still in X | The target constructed by a transform of a feature that stayed in the matrix | `y = df["revenue"] > t` with `revenue` (or `log_revenue`, `revenue_band`) still in `X`; a `drop(columns=["y"])` that misses a sibling column | blocking |
| 1.6 | Tuning confined to the training fold | Hyperparameter search that sees test rows | `GridSearchCV` fitted on `X` rather than `X_train`; a decision threshold chosen on the test set and then reported on that same set | blocking |
| 1.7 | The whole chain sits in one pipeline | Preprocessing steps applied by hand in sequence rather than wrapped | separate `fit_transform` calls chained by variable reassignment instead of `make_pipeline(...)` passed to the CV call. Score 1.7 on its own as major when the hand-rolled chain still fits on training rows only and transforms the held-out rows; when it is combined with any full-frame fit call, score it as 1.2 or 1.3 instead, blocking | major |

A pipeline is the mechanical defence for 1.2, 1.3 and 1.7 at once: it
guarantees that `fit` reaches only the training part of each fold and that
`transform` is what the held-out part receives.

## 2. Split hygiene, including group and temporal splits

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 2.1 | The split happens before anything else | Any statement that touches the data between load and split | imputation, winsorising, outlier removal or encoding above the `train_test_split` line | blocking |
| 2.2 | Grouped data uses a grouped split (L3.2) | Repeated measures of the same entity (client, firm, region, household) spread across folds | `KFold` or `train_test_split` on panel or longitudinal data where `GroupKFold`, `StratifiedGroupKFold` or a `groups=` argument is required | blocking |
| 2.3 | Ordered data uses an ordered split (L3.1) | A random split on data carrying a time index | `train_test_split(..., shuffle=True)` on a dated frame; `KFold(shuffle=True)` where `TimeSeriesSplit` is the correct splitter | blocking |
| 2.4 | The test set matches the target population (L3.3) | A test sample filtered, balanced or enriched relative to the population the claim is about | subsetting to complete cases only; dropping the hard segment; testing on one region and claiming national performance | major |
| 2.5 | Stratification where the class is rare | A random split leaving a fold with almost no positives | `train_test_split` without `stratify=y` at a 2 percent positive rate | major |
| 2.6 | Fold count and split sizes are stated | Numbers reported without the sample they rest on | no printed `len(X_train), len(X_test)`; a CV score quoted without the fold count | minor |
| 2.7 | Tuning and estimation do not share folds | The best search score reported as the generalisation estimate | `grid.best_score_` presented as the headline number with no outer held-out set | major |

## 3. Temporal leakage in forecasting

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 3.1 | No look-ahead in features | A feature computed with information the forecaster would not hold at the origin | a rolling statistic with `center=True`; a `shift(-1)`; `groupby.transform("mean")` over the full history; a full-sample standardisation of the series | blocking |
| 3.2 | Lags respect the release calendar | A macro or accounting variable used at its reference date rather than its publication date | GDP or CPI joined on the quarter it describes, not the date it was published; revised vintages used as if they were first prints | blocking |
| 3.3 | No shuffling anywhere in the chain | Any shuffle on a time-ordered frame | `shuffle=True`, `df.sample(frac=1)`, `KFold(shuffle=True)`, a re-sort that loses the ordering before splitting | blocking |
| 3.4 | Rolling-origin evaluation | A single fixed split used to claim stable forecasting performance | one tail holdout and nothing else; no expanding or sliding-origin loop; no per-origin score distribution | major |
| 3.5 | A gap where the horizon demands one | Horizon-h forecasts evaluated with the origin adjacent to the target | `TimeSeriesSplit` without `gap=` on a multi-step horizon | major |
| 3.6 | Target and horizon defined once, explicitly | Ambiguity about what is predicted how far ahead | the shift applied in two places with different values; the horizon named in prose but not in code | major |
| 3.7 | A naive baseline is scored | A model praised without the last-value or seasonal-naive comparison | no persistence baseline; MAPE quoted with nothing to beat | major |
| 3.8 | Exogenous regressors available at forecast time | Future values of a driver fed to a forecast of the target | a future regressor frame populated with realised values rather than forecast ones | blocking |

## 4. Class imbalance and resampling placement

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 4.1 | Resampling inside the fold, never before the split | The dataset balanced once, up front | `RandomUnderSampler().fit_resample(X, y)` or `SMOTE().fit_resample(X, y)` above the split or above the CV call | blocking |
| 4.2 | The evaluation fold keeps the natural class ratio | Both sides of the split balanced | resampled `X_resampled, y_resampled` passed straight to `cross_validate`, so the score measures a world that does not exist | blocking |
| 4.3 | Synthetic samples generated from training rows only | Interpolation using neighbours that land in the held-out fold | any oversampler fitted on the full frame; a generated point built from a row later used for evaluation | blocking |
| 4.4 | An imbalance-aware pipeline object is used | A plain scikit-learn `Pipeline` wrapping a sampler | `sklearn.pipeline.Pipeline` holding an `imblearn` sampler fails at fit (samplers have no `transform`); the usual workaround is to resample outside the pipeline before cross-validation, which reintroduces 4.1. Use `imblearn.pipeline.Pipeline`, which calls `fit_resample` on fit only and passes predict-time data through untouched | major |
| 4.5 | The imbalance response is justified | Resampling applied reflexively | resampling with no comparison against `class_weight="balanced"`, a shifted decision threshold, or leaving the ratio alone | minor |
| 4.6 | Accuracy is not the headline on an imbalanced target | 98 percent accuracy at a 2 percent positive rate | `accuracy_score` as the only reported metric; no balanced accuracy, recall or precision-recall curve | major |

## 5. Metric choice against the business question

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 5.1 | The metric matches the loss the decision carries | Squared error where the cost is linear, or the reverse | RMSE quoted when the client pays per unit of absolute error (MAE is the honest one); MAE quoted when large misses are disproportionately costly | major |
| 5.2 | Scale-dependent metrics not compared across series | RMSE compared between series of different magnitude | one RMSE table spanning products with different price levels; no MASE, sMAPE or other scaled alternative | major |
| 5.3 | MAPE not used on values near zero | A percentage error exploding on small denominators | `mean_absolute_percentage_error` on a series containing near-zero or zero actuals | major |
| 5.4 | PR-AUC where positives are rare | ROC-AUC presented as evidence of usable precision on a rare event | `roc_auc_score` alone at a 1 to 3 percent base rate, with no `average_precision_score` | major |
| 5.5 | Calibration checked when a probability is used as a number | Scores treated as probabilities without a reliability check | probabilities fed into an expected-value calculation with no calibration curve, no Brier score, no `CalibratedClassifierCV` | major |
| 5.6 | The operating threshold is not chosen where it is measured | The threshold tuned on the test scores | an `argmax` over thresholds on the test set, then the resulting F1 reported as performance | blocking |
| 5.7 | R-squared is not the sole regression evidence | A high R-squared with no error in the units the reader thinks in | `r2_score` alone; no residual plot; no MAE in currency or physical units | minor |
| 5.8 | The metric is reported with dispersion | A point estimate with no spread | a single CV mean with no standard deviation across folds, no interval, no repeat across seeds | major |
| 5.9 | A baseline anchors the number | A metric with nothing to compare against | no `DummyRegressor` or `DummyClassifier`, no incumbent model, no naive rule | major |

## 6. Feature provenance

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 6.1 | Every feature is available at prediction time (L2) | A column populated only after the outcome is known | a status flag set at case closure; a final-amount field; a post-event timestamp difference | blocking |
| 6.2 | No feature is a proxy of the target (L2) | A column mechanically derived from, or downstream of, the outcome | an identifier issued only to positives; a commentary field written after the event; a downstream system's derived score | blocking |
| 6.3 | Identifiers are excluded | Row keys, sequence numbers or file order carrying signal | `customer_id`, `row_number` or the index left in `X`; a tree model whose top split is on an identifier | blocking |
| 6.4 | Suspiciously dominant features are interrogated | One feature carrying nearly all the importance | an importance or coefficient plot with a single spike nobody explains; near-perfect performance on a hard problem | major |
| 6.5 | Feature engineering stated in plain language | Derived columns with no definition | a mass column-wise transform with no record of what the derived column means | minor |
| 6.6 | Missingness is not itself an outcome signal | A missing-indicator that encodes the label | an is-null flag on a column only ever populated for one class | major |
| 6.7 | Data dictionary reconciled | Features in the model that no source documents | columns present in `X` with no entry in the data dictionary, where one exists | minor |

## 7. Imputation placement

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 7.1 | Imputation fitted on training rows only | The mean, median or model used to fill learned from all rows | `df.fillna(df.median())` before the split; `SimpleImputer().fit_transform(X)` above the split | blocking |
| 7.2 | Iterative or model-based imputation inside the fold | `IterativeImputer`, kNN imputation or MICE run once over the whole dataset | an imputed dataset saved to disk, then split and modelled downstream; this leak persists across notebooks and is easy to inherit without noticing | blocking |
| 7.3 | The target itself is never imputed | Rows with a filled outcome kept in evaluation | `y` filled with a mean or a forward fill; interpolated outcome values scored as if observed | blocking |
| 7.4 | Time series filled forward, never backward | A gap filled with a later observation | a backward fill or an interpolation on a dated series used as a feature | blocking |
| 7.5 | The missingness mechanism is stated | Filling with no account of why values are missing | a blanket zero fill on a column where zero is also a valid observed value | major |
| 7.6 | Imputed values are flagged | No record of which values were observed | no indicator column, no count of filled values reported | minor |

## 8. Reproducibility

| # | Check | What to look for | Code symptom | Severity |
|---|---|---|---|---|
| 8.1 | The claimed number is reproducible | A quoted metric that a clean re-run does not return | the number in the memo matches no cell output; out-of-order execution counts; a metric typed into prose by hand | blocking |
| 8.2 | Seeds set on every stochastic object | `random_state` left at its default on estimators, splitters and samplers | `RandomForestClassifier()`, `KFold(shuffle=True)` or `train_test_split` with no `random_state`; `random_state=None` anywhere on a path that produces a reported number | major |
| 8.3 | Integer seeds where repeatability matters | A single generator instance passed around, mutating between calls, so repeated fits differ | one `RandomState` object handed to several estimators and splitters; integer seeds on CV splitters are the safer default | major |
| 8.4 | The result is stable across seeds | A conclusion resting on one lucky draw | one seed and no repeat; no `RepeatedKFold`; no spread across seeds reported | major |
| 8.5 | Library versions recorded | Results with no environment behind them | no dependency freeze, no lockfile, no versions printed; xgboost and scikit-learn defaults move between releases | minor |
| 8.6 | The data snapshot is identified | The input named only as the file | a bare relative path with no date, row count, hash or extraction query recorded | major |
| 8.7 | The notebook runs top to bottom | Hidden state producing the result | non-monotonic execution counts; variables defined in a deleted cell; a restart-and-run-all never attempted | major |

## 9. R branch: tidymodels, caret, base R

The same nine concerns, phrased in R. Run sections 1 to 8 using this table for
the code symptoms.

| # | Check | Code symptom in R | Severity |
|---|---|---|---|
| 9.1 | Recipes prepped on training data only | `prep(rec, training = full_df)`, or a recipe built and baked on the whole frame before `initial_split()`; the correct shape is `recipe()` on the training set bundled with the model spec in a tidymodels pipeline object, so every resample re-estimates it | blocking |
| 9.2 | Scaling and encoding inside the recipe, not applied by hand | `scale()`, `caret::preProcess()` or `model.matrix()` run on the full frame before splitting, instead of `step_normalize()` and `step_dummy()` inside the recipe | blocking |
| 9.3 | Imputation inside the recipe | `mice()` or `missForest()` run on the full frame and the imputed object saved; the correct shape is `step_impute_median()` or `step_impute_knn()` inside the recipe | blocking |
| 9.4 | Grouped resampling for repeated entities | `vfold_cv(df)` where `group_vfold_cv(df, group = client_id)` is required; `caret::createFolds` with no grouping on panel data | blocking |
| 9.5 | Temporal splits via rsample | `initial_split()` on a dated frame instead of `initial_time_split()`; `vfold_cv()` instead of `rolling_origin()` or `sliding_period()`; in caret, `trainControl(method = "cv")` instead of `method = "timeslice"` with `horizon` and `fixedWindow` | blocking |
| 9.6 | No look-ahead in dplyr feature code | `lead()` where `lag()` was meant; `mutate(z = (x - mean(x)) / sd(x))` computed over the full series; `zoo::rollmean(..., align = "center")`; a `slider` window that reaches forward | blocking |
| 9.7 | Imbalance handled inside the recipe | the frame rebalanced by hand before splitting; `themis::step_smote()` or `step_downsample()` are the correct home, and they skip at bake time on new data, which is the behaviour you want | blocking |
| 9.8 | Metrics chosen deliberately | `yardstick::metric_set()` carrying only `rmse` where `mae` fits the decision, or only `roc_auc` where `pr_auc` is required on a rare event; `caret::confusionMatrix` accuracy as the headline on an imbalanced target | major |
| 9.9 | Calibration checked | probabilities from `predict(type = "prob")` used in an expected-value calculation with no reliability plot or Brier score | major |
| 9.10 | Identifiers excluded | an ID column left in the formula through `y ~ .`; `update_role(id, new_role = "ID")` missing from the recipe | blocking |
| 9.11 | Seeds set before every resampling and fitting call | one `set.seed()` at the top of a long script with parallel workers underneath and no reproducible RNG kind; no `set.seed()` immediately before `vfold_cv()` and before `tune_grid()` | major |
| 9.12 | Environment captured | no `sessionInfo()` output and no `renv.lock`; results attributed to a script with no record of package versions | minor |
| 9.13 | The script runs clean from a fresh session | results produced with objects from earlier runs still in the global environment; a saved workspace restored on start | major |
| 9.14 | Base R holdouts done in the right order | `sample()` indices drawn after a full-frame `scale()` or after an `na.omit()` on columns that differ between the two sides | blocking |

---

## Using this rubric

Walk the sections in order. Sections 1 to 3 decide the verdict most of the
time: a leak found there makes everything downstream a measurement of the leak.
Sections 4 to 7 decide whether the number answers the question that was asked.
Section 8 decides whether the number survives contact with a second person.

For each finding record `severity | finding | file:line | fix`. If a check
cannot be evaluated from the artefact, record it as not determinable and say
what would settle it; never record it as a pass.
