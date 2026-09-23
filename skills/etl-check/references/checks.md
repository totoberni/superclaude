# etl-check: the checks in pointblank

One paragraph per check, each with the call that implements it. Every API name below was read
from the pointblank reference index at https://posit-dev.github.io/pointblank/reference/ and,
where a signature is quoted, from that symbol's own reference page. The names marked
UNVERIFIED are not pointblank symbols and come from another library or from plain Python.

All checks attach to one `Validate` object so a single interrogation produces one HTML report:

```python
import pointblank as pb

validation = pb.Validate(
    data=table,
    tbl_name="clients_2026Q1",
    label="pre-flight",
    thresholds=pb.Thresholds(warning=0.01, error=0.0, critical=0.0),
)
```

Verified signature: `Validate(data, reference=None, tbl_name=None, label=None, thresholds=None,
actions=None, final_actions=None, brief=None, lang=None, locale=None, owner=None,
consumers=None, version=None)` and `Thresholds(warning=None, error=None, critical=None)`.
`lang="it"` is available if the report is a client deliverable.

## 1. Schema drift against the previous snapshot

Build a `Schema` from the expected column names and dtypes, then assert it with
`col_schema_match`. The expected schema is the snapshot the previous successful run wrote;
`schema_from_tbl` produces it from a table, and the snapshot itself is a plain JSON file written
by the template (JSON serialisation is ours, not a pointblank feature). `complete=True` catches
added and dropped columns, `in_order=False` tolerates reordering, and
`full_match_dtypes=False` tolerates a widening dtype when that is acceptable for the pipeline.

```python
expected = pb.Schema(columns=[("client_id", "Int64"), ("period", "String"), ("revenue", "Float64")])
validation.col_schema_match(expected, complete=True, in_order=False)
```

Verified: `Schema(columns=None, tbl=None, **kwargs)`; `Validate.col_schema_match(schema,
complete=True, in_order=True, case_sensitive_colnames=True, case_sensitive_dtypes=True,
full_match_dtypes=True, pre=None, thresholds=None, actions=None, brief=None, active=True,
dimension=None)`; `schema_from_tbl(tbl, *, infer_constraints=True, categorical_threshold=20,
detect_presets=True, sample_size=None)`.

## 2. Null keys

Every key column must be fully populated. `col_vals_not_null` takes one column or a list, so a
composite key is one step per column when you want per-column evidence in the report, or a single
step over the list when you only need the verdict.

```python
for key in key_columns:
    validation.col_vals_not_null(columns=key)
```

Verified: `Validate.col_vals_not_null` exists in the reference index. The `columns` parameter name
follows the pointblank column-step convention; the column selector helpers `pb.col`,
`pb.starts_with`, `pb.ends_with`, `pb.contains`, `pb.matches`, `pb.everything`, `pb.first_n`,
`pb.last_n` are all verified names if a pattern is needed instead of a literal list.

## 3. Duplicate keys

`rows_distinct` with `columns_subset` set to the key is the duplicate-key check; with
`columns_subset=None` it is a whole-row duplicate check, which is worth adding as a second step
because a fully duplicated row usually means the extract was appended twice.

```python
validation.rows_distinct(columns_subset=key_columns)
validation.rows_distinct()
```

Verified: `Validate.rows_distinct(columns_subset=None, pre=None, segments=None, thresholds=None,
actions=None, brief=None, active=True, dimension=None)`. `Validate.rows_complete` is the sibling
step for rows with no missing values anywhere.

## 4. Idempotency of the write

pointblank cannot read your job source, so this check has two halves. The static half is ours:
grep the job for the write statement and confirm it is a `MERGE INTO ... ON <key>` or an
`INSERT OVERWRITE` of a bounded partition (Python writer: `mode("overwrite")` with
`replaceWhere`, never a bare `mode("append")` on a re-runnable job). The dynamic half is
pointblank: the key the MERGE claims to be unique on must actually be unique in the incoming
data, which is check 3, and the row count of a dry re-read must match the count of the first read.

```python
validation.row_count_match(count=expected_row_count, tol=0.0)
```

Verified: `Validate.row_count_match(count, tol=0, inverse=False, pre=None, thresholds=None,
actions=None, brief=None, active=True, dimension=None)`; `Validate.col_count_match` is the column
equivalent; `pb.get_row_count` and `pb.get_column_count` are verified helpers for reading the
counts outside the validation plan. The grep half is UNVERIFIED as a pointblank feature because
it is not one: it is plain text search over the job source.

## 5. Partitioning sanity

Three assertions. The partition column exists (`col_exists`); it is never null
(`col_vals_not_null`); and its cardinality is inside the sane band for the volume, which is a
computed number rather than a row predicate, so it goes through `specially` with a callable that
returns a boolean, or is computed before the plan and asserted with `col_vals_in_set` against the
partitions the job claims to touch.

```python
validation.col_exists(columns=partition_col)
validation.col_vals_not_null(columns=partition_col)
validation.col_vals_in_set(columns=partition_col, set=expected_partitions)
validation.specially(expr=lambda df: partition_cardinality_is_sane(df, partition_col))
```

Verified: `Validate.col_exists`, `Validate.col_vals_in_set`, `Validate.col_vals_not_null`,
`Validate.specially`, `Validate.conjointly`, `Validate.col_vals_expr` are all in the reference
index. The exact keyword names of `specially` (`expr`) and `col_vals_in_set` (`set`) are
UNVERIFIED; confirm on their reference pages before shipping a script that uses them.

## 6. Databricks cost guard

Row and byte estimate before the read, warehouse size, and a partition filter in the WHERE
clause. `pb.get_row_count(table)` gives the row estimate for an already-connected table, and
`pb.preview(table)` plus `pb.col_summary_tbl(table)` are the cheap profiling calls that show what
the read will cost. The "no full scan without a partition filter" half is again a static check on
the SQL text: the query must contain a predicate on the partition column of the source.

```python
n_rows = pb.get_row_count(table)
validation.row_count_match(count=budget_rows, tol=0.2, inverse=True)  # fallisce se supera il budget
```

Verified: `pb.get_row_count`, `pb.get_column_count`, `pb.preview`, `pb.col_summary_tbl`,
`pb.missing_vals_tbl`, `pb.DataScan` are all in the reference index. Whether `inverse=True` on
`row_count_match` is the right way to express an upper bound is UNVERIFIED semantics; the safe
form is to compute `n_rows` first and fail the script directly.

## Report and exit

```python
validation.interrogate()
validation.get_tabular_report().write_raw_html(report_path)
ok = validation.all_passed()
```

Verified: `Validate.interrogate`, `Validate.get_tabular_report(title=":default:",
incl_header=None, incl_footer=None, incl_footer_timings=None, incl_footer_notes=None,
incl_dimensions=None)`, `Validate.all_passed`, `Validate.n_failed`, `Validate.n_passed`,
`Validate.f_passed`, `Validate.get_step_report`, `Validate.get_json_report`,
`Validate.get_data_extracts`, `Validate.get_sundered_data`, `Validate.assert_passing`.
`get_tabular_report` returns a great-tables `GT` object; `GT.write_raw_html`, `GT.as_raw_html` and
`GT.gtsave` were verified against the great-tables reference index, not the pointblank one.
Note that `pb.write_file(validation, filename, path=None, keep_tbl=False, keep_extracts=False,
quiet=False)` serialises the validation OBJECT, it does not write the HTML report.

## Connecting to data

`pb.connect_to_table(connection_string)` returns an Ibis table and requires a `::table_name`
suffix. Verified formats from its reference page: `duckdb:///path/to/database.ddb::table_name`,
`sqlite:///path/to/database.db::table_name`,
`postgresql://user:password@localhost:5432/database::table_name`,
`mysql://...`, `bigquery://project/dataset::table_name`,
`snowflake://user:password@account/database/schema::table_name`. **No Databricks form is
documented there**, so for a warehouse table read the slice with `databricks-sql-connector` into
a Polars or Pandas frame and pass that frame to `Validate(data=...)`. Never put a password in a
connection string in a repo; use a `~/.databrickscfg` profile or environment variables.

Other verified entry points worth knowing: `pb.load_dataset` (bundled sample data),
`pb.yaml_interrogate` and `pb.validate_yaml` (run a plan written as YAML),
`Validate.to_yaml` and `Validate.to_code` (export a plan), `pb.Actions`, `pb.FinalActions` and
`pb.send_slack_notification` (act on failure).

## Forbidden

`pb.DraftValidation` sends a data sample to an external LLM provider to draft a plan. It is
forbidden here. The same applies to `Validate.from_prompt`, `Validate.prompt` and
`Validate.suggest_improvements`, which are LLM-backed for the same reason. All four are verified
to exist; they are banned by policy, not absent.
