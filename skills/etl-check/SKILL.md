---
name: etl-check
description: "Use when pre-flight checking a Databricks ETL pipeline before a job run"
category: domain
user-invocable: true
argument-hint: "<table-path-or-ref> [--keys col,col] [--prev-schema PATH] [--partition-col COL] [--report PATH.html]"
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# ETL Pre-Flight Check

Run the checks as CODE, never as a prose checklist. Every row of the output table must cite a
number produced by an actual interrogation, not a judgement.

## When to use

- Before running or re-running a Databricks job, a notebook write, or an Azure pipeline stage.
- After an upstream source changed (new export, new vendor file, new Unity Catalog table version).
- Before promoting a pipeline from dev to prod, or before a backfill.

Not for: post-hoc incident analysis (use `/debugging`), model quality (use a model rubric),
figure or report output.

## Inputs

| Input | Required | Form |
|---|---|---|
| Table reference | yes | Local CSV or Parquet path, a DuckDB table, or a Unity Catalog `catalog.schema.table` |
| Key columns | yes for key checks | Comma-separated business or merge key, e.g. `--keys client_id,period` |
| Previous schema snapshot | optional | JSON written by a previous run (`--prev-schema`); absent on first run |
| Partition column | optional | The column the target is partitioned by (`--partition-col`) |
| Expected row count | optional | From the previous run, for the drift and cost guard |

If the key columns are unknown, STOP and ask. Guessing the key invalidates the duplicate,
idempotency and join checks.

## Check list

### (a) Covered by Anthropic's `data` plugin

If the `data` plugin is enabled in this session, its `/validate` skill already covers: null
analysis, duplicate detection, join explosion, survivorship bias, and denominator sanity.
**Use it if present; otherwise run section (b), which re-implements the null and duplicate
parts in code.** Do not assume the plugin is installed: check the session skill listing first.
Section (b) is always run, plugin or not, because nothing in the plugin covers drift,
idempotency, partitioning or cost.

### (b) Ours

1. **Schema drift against the previous snapshot.** Column set, order and dtypes compared with the
   snapshot from the last successful run. Added, dropped, renamed and retyped columns are all
   reported. No snapshot means the run writes the first one and the check reports `BASELINE`.
2. **Null and duplicate keys.** Each key column must be fully non-null; the key tuple must be
   unique. A duplicate key is the single most common cause of a join explosion downstream.
3. **Idempotency of the write.** The target write must be a `MERGE` on a declared key or an
   `INSERT OVERWRITE` of a bounded partition. An unconditional `INSERT INTO` or `append` on a
   re-runnable job fails this check. Evidence is the write statement or the DataFrame writer mode
   read from the job source, quoted verbatim.
4. **Partitioning sanity.** The partition column exists, is non-null, has a cardinality that fits
   the data volume (a warning below roughly 1 file per partition or above a few thousand
   partitions), and the incoming batch touches the partitions the job claims to touch.
5. **Databricks cost guard.** Row and byte estimate of the read; warehouse size versus that
   estimate; and no full scan of a partitioned source without a partition filter in the WHERE
   clause. A query that scans a whole fact table on a Large warehouse is a REWORK, not a warning.

## How to run

The template is `~/.claude/skills/etl-check/templates/validate_template.py`. Copy it into the
project (not into `~/.claude`), then run it with the project interpreter.

```bash
# Le dipendenze vivono nel venv di progetto, mai in ~/.claude/.venv
uv pip install --python <project>/.venv/Scripts/python.exe pointblank polars duckdb

python <project>/scripts/validate_etl.py \
  --table data/raw/clients_2026Q1.parquet \
  --keys client_id,period \
  --partition-col period \
  --prev-schema .etl-check/schema_prev.json \
  --report reports/etl_check.html
```

The script exits 0 only when the verdict is CLEAN (no FAIL and no WARN rows) and 1 otherwise,
so it drops into a job step or a pre-commit gate unchanged.

Backends:

- **Local CSV, Parquet or Excel extracts**: DuckDB (`pip install duckdb`) is the recommended
  local backend; it reads the files in place with SQL and hands pointblank a table without a
  server. Polars or Pandas work equally well for anything that fits in memory.
- **Databricks warehouse tables**: `databricks-sql-connector`, reading the slice under test into
  a Polars or Pandas frame, then validating that frame. Credentials come from a
  `~/.databrickscfg` profile or from environment variables, NEVER from a file in the repo and
  never from a literal in the script.

## Hard rules

- **`DraftValidation` is forbidden.** `pointblank.DraftValidation` sends a sample of the data to
  an external LLM provider to draft a validation plan. Client data must not leave the machine.
  Write the plan by hand. The same applies to `Validate.from_prompt`, `Validate.prompt` and
  `Validate.suggest_improvements`, which are LLM-backed for the same reason.
- No credentials in the repo, in the script, or in the report.
- Never weaken a threshold to make the check pass. A failing check is the deliverable.
- Do not run the pipeline as part of the check. The check is read-only on the source.

## Output

First line, then the table. The token is the deterministic-checker use of the shared contract in
`_shared/verdict-schema.md`: `CLEAN` requires `blocking=0 major=0`.

```
VERDICT: REWORK blocking=1 major=1 minor=0

| Check | Result | Evidence |
|---|---|---|
| Schema drift | FAIL | column `revenue_eur` dropped, `revenue` added (float64); prev-schema .etl-check/schema_prev.json |
| Null keys | PASS | 0/48210 null in client_id, period |
| Duplicate keys | FAIL | 37 duplicate (client_id, period) tuples; extract in reports/etl_check.html step 4 |
| Write idempotency | PASS | MERGE INTO silver.clients ON t.client_id = s.client_id (jobs/load_clients.py:88) |
| Partitioning | WARN | 1204 partitions for 48210 rows (40 rows/partition) |
| Cost guard | PASS | 48210 rows, 6.1 MB, WHERE period = '2026Q1' on partition column; warehouse 2X-Small |
```

Severity: schema drift, duplicate or null keys, non-idempotent write and an unfiltered full scan
are `blocking`. Partition cardinality and a row-count deviation inside tolerance are `major` or
`minor`. Always name the file, the step number in the HTML report, or the `file:line` of the
write statement as evidence.

## References

- Per-check pointblank calls and verified API names: `references/checks.md`
- Runnable skeleton: `templates/validate_template.py`
- pointblank docs: https://posit-dev.github.io/pointblank/
