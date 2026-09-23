#!/usr/bin/env python
"""ETL pre-flight check: schema drift, key integrity, idempotency, partitioning, cost guard.

Copiare questo file nel progetto (mai in ~/.claude) e adattare le sezioni marcate ADATTARE.
Dipendenze nel venv di progetto:  uv pip install pointblank polars duckdb
Per le tabelle di warehouse aggiungere: databricks-sql-connector

VIETATO: pointblank.DraftValidation, Validate.from_prompt, Validate.prompt,
Validate.suggest_improvements. Inviano un campione dei dati a un provider LLM esterno.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pointblank as pb

# Soglie di default: nessuna tolleranza sugli errori, warning all'1% di righe fallite.
DEFAULT_THRESHOLDS = pb.Thresholds(warning=0.01, error=0.0, critical=0.0)

# Banda di cardinalita accettabile per una colonna di partizione.
MIN_ROWS_PER_PARTITION = 1_000
MAX_PARTITIONS = 5_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ETL pre-flight check")
    parser.add_argument("--table", required=True,
                        help="Percorso CSV/Parquet, oppure catalog.schema.table su Databricks")
    parser.add_argument("--keys", required=True,
                        help="Colonne chiave separate da virgola, es. client_id,period")
    parser.add_argument("--partition-col", default=None)
    parser.add_argument("--prev-schema", default=None,
                        help="Snapshot JSON dello schema dell'ultimo run riuscito")
    parser.add_argument("--report", default="etl_check.html")
    parser.add_argument("--expected-rows", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Cost guard: soglia di righe oltre la quale il check fallisce")
    parser.add_argument("--databricks-profile", default="DEFAULT",
                        help="Profilo in ~/.databrickscfg; nessuna credenziale nel repo")
    return parser.parse_args()


def load_local_file(path: Path) -> Any:
    """Legge CSV/Parquet con DuckDB e restituisce un DataFrame Polars (fallback Pandas)."""
    import duckdb

    reader = "read_csv_auto" if path.suffix.lower() == ".csv" else "read_parquet"
    relation = duckdb.sql(f"SELECT * FROM {reader}('{path.as_posix()}')")
    try:
        return relation.pl()
    except ImportError:
        return relation.df()


def load_databricks_table(table_ref: str, profile: str):
    """Legge una tabella Unity Catalog via databricks-sql-connector.

    Le credenziali arrivano dal profilo ~/.databrickscfg o dalle variabili d'ambiente
    DATABRICKS_SERVER_HOSTNAME / DATABRICKS_HTTP_PATH / DATABRICKS_TOKEN.
    ADATTARE: aggiungere il filtro di partizione, non leggere mai la tabella intera.
    """
    import os

    from databricks import sql as dbsql

    connection = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    with connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {table_ref}")  # ADATTARE: aggiungere WHERE sulla partizione
        return cursor.fetchall_arrow().to_pandas()


def load_table(table_ref: str, profile: str) -> Any:
    path = Path(table_ref)
    if path.exists():
        return load_local_file(path)
    if table_ref.count(".") >= 1:
        return load_databricks_table(table_ref, profile)
    raise SystemExit(f"tabella non risolvibile: {table_ref}")


def column_types(table: Any) -> dict[str, str]:
    """Mappa colonna -> dtype, sia per Polars sia per Pandas."""
    if hasattr(table, "schema") and hasattr(table, "columns"):
        return {name: str(dtype) for name, dtype in zip(table.columns, table.dtypes)}
    return {name: str(dtype) for name, dtype in table.dtypes.items()}


def schema_drift(current: dict[str, str], snapshot_path: str | None) -> tuple[str, str]:
    """Confronta lo schema corrente con lo snapshot del run precedente."""
    if not snapshot_path or not Path(snapshot_path).exists():
        return "BASELINE", "nessuno snapshot precedente; questo run ne scrive uno"

    previous = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    added = sorted(set(current) - set(previous))
    dropped = sorted(set(previous) - set(current))
    retyped = sorted(c for c in set(current) & set(previous) if current[c] != previous[c])
    if not (added or dropped or retyped):
        return "PASS", f"{len(current)} colonne invariate vs {snapshot_path}"
    return "FAIL", f"added={added} dropped={dropped} retyped={retyped}"


def partition_sanity(table: Any, partition_col: str | None) -> tuple[str, str]:
    """Cardinalita della colonna di partizione rapportata al volume dei dati."""
    if not partition_col:
        return "SKIP", "nessuna colonna di partizione dichiarata"

    n_rows = pb.get_row_count(table)
    n_partitions = len(set(_column_values(table, partition_col)))
    rows_per_partition = n_rows / max(n_partitions, 1)
    evidence = f"{n_partitions} partizioni per {n_rows} righe ({rows_per_partition:.0f} righe/part.)"
    if n_partitions > MAX_PARTITIONS or rows_per_partition < MIN_ROWS_PER_PARTITION:
        return "WARN", evidence
    return "PASS", evidence


def _column_values(table: Any, column: str) -> list[Any]:
    if hasattr(table, "get_column"):
        return table.get_column(column).to_list()
    return table[column].tolist()


def cost_guard(table: Any, max_rows: int | None) -> tuple[str, str]:
    """Stima di righe letta prima del job; il filtro di partizione va verificato nel SQL."""
    n_rows = pb.get_row_count(table)
    if max_rows is None:
        return "WARN", f"{n_rows} righe lette; nessun budget dichiarato (--max-rows)"
    if n_rows > max_rows:
        return "FAIL", f"{n_rows} righe lette contro un budget di {max_rows}"
    return "PASS", f"{n_rows} righe lette entro il budget di {max_rows}"


def build_validation(table: Any, table_name: str, keys: list[str], partition_col: str | None,
                     expected_rows: int | None) -> Any:
    """Piano di validazione pointblank: chiavi nulle, chiavi duplicate, conteggio righe."""
    validation = pb.Validate(
        data=table,
        tbl_name=table_name,
        label="etl-check pre-flight",
        thresholds=DEFAULT_THRESHOLDS,
    )
    for key in keys:
        validation = validation.col_vals_not_null(columns=key)
    validation = validation.rows_distinct(columns_subset=keys)
    if partition_col:
        validation = validation.col_exists(columns=partition_col)
        validation = validation.col_vals_not_null(columns=partition_col)
    if expected_rows is not None:
        validation = validation.row_count_match(count=expected_rows)
    return validation.interrogate()


def render_report(validation: Any, report_path: str) -> None:
    """get_tabular_report restituisce un oggetto great-tables; write_raw_html scrive l'HTML."""
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    validation.get_tabular_report().write_raw_html(report_path)


def emit(rows: list[tuple[str, str, str]]) -> int:
    """Stampa il token di verdetto e la tabella check/risultato/evidenza.

    Ogni esito diverso da PASS, WARN o FAIL vale WARN: un refuso o un check non
    adattato non puo' passare in silenzio come se fosse superato.
    """
    severity = [result if result in ("PASS", "WARN", "FAIL") else "WARN" for _, result, _ in rows]
    blocking = sum(1 for result in severity if result == "FAIL")
    major = sum(1 for result in severity if result == "WARN")
    verdict = "CLEAN" if blocking == 0 and major == 0 else "REWORK"
    print(f"VERDICT: {verdict} blocking={blocking} major={major} minor=0")
    print()
    print("| Check | Result | Evidence |")
    print("|---|---|---|")
    for check, result, evidence in rows:
        print(f"| {check} | {result} | {evidence} |")
    return 0 if verdict == "CLEAN" else 1


def main() -> int:
    args = parse_args()
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    table = load_table(args.table, args.databricks_profile)
    current_schema = column_types(table)

    validation = build_validation(table, args.table, keys, args.partition_col, args.expected_rows)
    render_report(validation, args.report)

    key_result = "PASS" if validation.all_passed() else "FAIL"
    failed_steps = {step: n for step, n in validation.n_failed(i=None).items() if n}
    key_evidence = f"righe fallite per step {failed_steps or 'nessuno'}; dettaglio in {args.report}"

    drift_result, drift_evidence = schema_drift(current_schema, args.prev_schema)
    rows = [
        ("Schema drift", drift_result, drift_evidence),
        ("Null and duplicate keys", key_result, key_evidence),
        # ADATTARE: sostituire con il grep reale sullo statement di scrittura del job.
        ("Write idempotency", "WARN",
         "controllo non adattato: verificare a mano MERGE INTO o INSERT OVERWRITE nel job"),
        ("Partitioning", *partition_sanity(table, args.partition_col)),
        ("Cost guard", *cost_guard(table, args.max_rows)),
    ]
    exit_code = emit(rows)

    # Lo snapshot si aggiorna quando lo schema non e' in drift, anche al primo run (BASELINE):
    # legarlo all'exit code impedirebbe per sempre la creazione del riferimento iniziale.
    if drift_result != "FAIL" and args.prev_schema:
        Path(args.prev_schema).parent.mkdir(parents=True, exist_ok=True)
        Path(args.prev_schema).write_text(json.dumps(current_schema, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
