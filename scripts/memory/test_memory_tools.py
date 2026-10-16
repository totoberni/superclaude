"""Test per gli strumenti di memoria: alias dei tier, `sql`, `stats`, espansione di `~`
e i port Python scan_mem_matrix / mem_health.

Ogni test lavora su un DB temporaneo esplicito (`--db` o MEMORY_DB_PATH): il DB live
non viene mai aperto. Gli embedding sono reali (modello in cache, HF_HUB_OFFLINE=1).
I timestamp `updated` sono fissati a mano e MEM_NOW blocca "adesso", cosi' i
punteggi sono deterministici.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# plan_index calcola i suoi percorsi da Path.home() all'import: va importato prima di
# qualunque test che ridefinisce USERPROFILE
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "plan"))
import mem_health  # noqa: E402
import memory_db as mdb  # noqa: E402
import plan_index  # noqa: E402
import scan_mem_matrix as scan  # noqa: E402

_MEMDB = os.path.join(_HERE, "memory_db.py")
_SCAN = os.path.join(_HERE, "scan_mem_matrix.py")
_HEALTH = os.path.join(_HERE, "mem_health.py")

_NOW = "2026-06-01T00:00:00+00:00"
_OLD = "2026-01-01T00:00:00+00:00"
_NEW = "2026-05-20T00:00:00+00:00"

_DUP_TEXT = "alpha widget calibration procedure for the sensor drift bench"
_FAT_TEXT = "gamma " + " ".join(f"cword{i}" for i in range(60))
_EXEMPT_TEXT = "<!-- budget-exempt -->\n" + " ".join(f"dword{i}" for i in range(60))
_CLASS_TEXT = "epsilon note about latex paragraph macros"

# (path, tier come passato dal chiamante, type, name, description, text, updated)
_ROWS = [
    ("instance/a/dup_one.md", "instance", "feedback", "dup_one", "first copy", _DUP_TEXT, _OLD),
    ("instance/a/dup_two.md", "Instance ", "feedback", "dup_two", "second copy",
     _DUP_TEXT + " again", _OLD),
    ("shared-projects/_/fat_row.md", "shared", "project", "fat_row", "a fat body",
     _FAT_TEXT, _NEW),
    ("shared-global/_/exempt_row.md", "GLOBAL", "reference", "exempt_row", "exempt fat body",
     _EXEMPT_TEXT, _NEW),
    ("class/w/no_desc.md", "class", "project", "no_desc", "", _CLASS_TEXT, _NEW),
]


def _set_updated(db, name, stamp):
    conn = mdb._connect(db)
    try:
        conn.execute("UPDATE memories SET updated = ? WHERE name = ?", (stamp, name))
        conn.commit()
    finally:
        conn.close()


def _build(db):
    mdb.init_db(db)
    for path, tier, mtype, name, desc, text, stamp in _ROWS:
        mdb.upsert(path=path, tier=tier, agent=None, type=mtype, name=name,
                   description=desc, text=text, db_path=db)
        _set_updated(db, name, stamp)
    return db


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """DB popolato di SOLA LETTURA per i test (nessun test lo modifica)."""
    return _build(tmp_path_factory.mktemp("tools") / ".memory.db")


@pytest.fixture()
def rwdb(tmp_path):
    """DB piccolo e scrivibile, uno per test."""
    p = tmp_path / ".memory.db"
    mdb.init_db(p)
    mdb.upsert(path="instance/_/seed.md", tier="instance", agent=None, type="feedback",
               name="seed", description="seed row", text="seed row about docker compose",
               db_path=p)
    return p


@pytest.fixture(scope="module")
def empty_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("empty") / ".memory.db"
    mdb.init_db(p)
    return p


def _run(script, *args, env=None):
    full_env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONUTF8="1")
    for key in ("MEM_OVERSIZE_BYTES", "MEM_STALE_DAYS", "MEM_DUP_SIM_MIN",
                "MEM_CORPUS_BYTES_MAX", "MEM_INCOMPLETE_CAP", "MEM_NOW",
                "MEMORY_DB_PATH", "CLAUDE_DIR", "SC_MEM_REALM"):
        full_env.pop(key, None)
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, script, *args], capture_output=True,
                          text=True, encoding="utf-8", env=full_env)
    return proc.returncode, proc.stdout, proc.stderr


def _cli(db, *args, env=None):
    return _run(_MEMDB, "--db", str(db), *args, env=env)


def _tier_of(db, name):
    cols, rows = mdb.run_sql(f"SELECT tier, path FROM memories WHERE name = '{name}'", db)
    return rows[0]


# --- normalize_tier ----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("shared", "shared-projects"), ("global", "shared-global"),
    ("  Shared ", "shared-projects"), ("GLOBAL", "shared-global"),
    ("instance", "instance"), ("class", "class"), ("shared-projects", "shared-projects"),
    ("Shared-Global", "shared-global"), ("archive", "archive"),
])
def test_normalize_tier_accepts(raw, expected):
    assert mdb.normalize_tier(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "projects", "shared_global", "long-term", None])
def test_normalize_tier_rejects_unknown_listing_valid(raw):
    with pytest.raises(ValueError) as exc:
        mdb.normalize_tier(raw)
    for tier in mdb.CANONICAL_TIERS:
        assert tier in str(exc.value)


# --- Alias nell'API Python ---------------------------------------------------


def test_api_upsert_stores_canonical_tier(db):
    assert _tier_of(db, "fat_row")[0] == "shared-projects"
    assert _tier_of(db, "exempt_row")[0] == "shared-global"
    assert _tier_of(db, "dup_two")[0] == "instance"


def test_api_search_tier_alias(db):
    hits = mdb.search("gamma cword1", k=5, mode="fts", db_path=db, tier="shared")
    assert [h["name"] for h in hits] == ["fat_row"]
    many = mdb.search_many(["gamma cword1", "cword2"], k=5, mode="fts", db_path=db,
                           tier=" Shared ")
    assert [h["name"] for h in many] == ["fat_row"]


def test_api_similar_tier_alias(db):
    conn = mdb._connect(db)
    try:
        rid = conn.execute("SELECT id FROM memories WHERE name='dup_one'").fetchone()[0]
    finally:
        conn.close()
    res = mdb.similar(rid, k=8, tier="global", db_path=db)
    assert [r["name"] for r in res] == ["exempt_row"]


def test_api_retier_alias(rwdb):
    info = mdb.retier(1, "global", db_path=rwdb)
    assert info["new_tier"] == "shared-global"
    assert info["new_path"] == "shared-global/_/seed.md"


def test_api_retier_archive_alias_case_still_rejected(rwdb):
    with pytest.raises(ValueError, match="archive"):
        mdb.retier(1, " Archive ", db_path=rwdb)


def test_api_rejects_unknown_tier_everywhere(rwdb):
    with pytest.raises(ValueError, match="unknown tier"):
        mdb.upsert(path="x/y.md", tier="bogus", agent=None, type="user", name="y",
                   description="d", text="t", db_path=rwdb)
    with pytest.raises(ValueError, match="unknown tier"):
        mdb.search("seed", mode="fts", db_path=rwdb, tier="bogus")
    with pytest.raises(ValueError, match="unknown tier"):
        mdb.search_many(["seed", "docker"], mode="fts", db_path=rwdb, tier="bogus")
    with pytest.raises(ValueError, match="unknown tier"):
        mdb.similar(1, tier="bogus", db_path=rwdb)
    with pytest.raises(ValueError, match="unknown tier"):
        mdb.retier(1, "bogus", db_path=rwdb)
    assert mdb.run_sql("SELECT COUNT(*) FROM memories", rwdb)[1] == [(1,)]


# --- Alias nella CLI ---------------------------------------------------------


def test_cli_upsert_alias_sets_tier_and_default_path(rwdb, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("cloudflare workers deploy note", encoding="utf-8")
    rc, out, err = _cli(rwdb, "upsert", "--tier", "Shared", "--type", "project",
                        "--name", "cf_note", "--description", "cf", "--text-file", str(body))
    assert rc == 0, err
    assert "path=shared-projects/_/cf_note.md" in out
    assert _tier_of(rwdb, "cf_note") == ("shared-projects", "shared-projects/_/cf_note.md")


def test_cli_list_tier_alias(db):
    rc, out, _ = _cli(db, "list", "--tier", "global")
    assert rc == 0
    assert "exempt_row" in out and "fat_row" not in out
    rc, out, _ = _cli(db, "list", "--tier", "shared")
    assert "fat_row" in out and "exempt_row" not in out


def test_cli_search_tier_alias(db):
    rc, out, err = _cli(db, "search", "cword1 dword1", "--mode", "fts", "--tier", "global",
                        "--json")
    assert rc == 0, err
    assert [r["name"] for r in json.loads(out)] == ["exempt_row"]


def test_cli_similar_tier_alias(db):
    rc, out, err = _cli(db, "similar", "--name", "dup_one", "--tier", "shared", "--json")
    assert rc == 0, err
    assert [r["name"] for r in json.loads(out)] == ["fat_row"]


def test_cli_retier_alias(rwdb):
    rc, out, err = _cli(rwdb, "retier", "--name", "seed", "--tier", "shared")
    assert rc == 0, err
    assert _tier_of(rwdb, "seed") == ("shared-projects", "shared-projects/_/seed.md")


@pytest.mark.parametrize("args", [
    ("upsert", "--tier", "bogus", "--type", "user", "--name", "n", "--description", "d",
     "--text-stdin"),
    ("list", "--tier", "bogus"),
    ("search", "seed", "--mode", "fts", "--tier", "bogus"),
    ("similar", "--id", "1", "--tier", "bogus"),
    ("retier", "--id", "1", "--tier", "bogus"),
])
def test_cli_unknown_tier_exits_2_and_lists_valid(rwdb, args):
    rc, out, err = _cli(rwdb, *args)
    assert rc == 2
    assert "unknown tier 'bogus'" in err
    assert "shared-projects" in err and "shared-global" in err
    assert _tier_of(rwdb, "seed")[0] == "instance"


def test_cli_archive_round_trip_with_aliased_tier(rwdb, tmp_path):
    body = tmp_path / "rt.md"
    body.write_text("round trip body about matplotlib mathtext", encoding="utf-8")
    rc, _, err = _cli(rwdb, "upsert", "--tier", "global", "--type", "reference",
                      "--name", "rt", "--description", "rt", "--text-file", str(body))
    assert rc == 0, err
    original = _tier_of(rwdb, "rt")
    assert original == ("shared-global", "shared-global/_/rt.md")

    rc, _, err = _cli(rwdb, "archive", "--name", "rt")
    assert rc == 0, err
    assert _tier_of(rwdb, "rt") == ("archive", "archive/shared-global::shared-global/_/rt.md")
    # fuori dal richiamo attivo, ma raggiungibile con --tier archive (anche in maiuscolo)
    rc, out, _ = _cli(rwdb, "search", "matplotlib", "--mode", "fts", "--json")
    assert json.loads(out) == []
    rc, out, _ = _cli(rwdb, "search", "matplotlib", "--mode", "fts", "--tier", "ARCHIVE",
                      "--json")
    assert [r["name"] for r in json.loads(out)] == ["rt"]

    rc, _, err = _cli(rwdb, "archive", "--name", "rt", "--unarchive")
    assert rc == 0, err
    assert _tier_of(rwdb, "rt") == original


# --- sql ---------------------------------------------------------------------


def test_sql_happy_path_tsv(db):
    rc, out, err = _cli(db, "sql", "SELECT name, tier FROM memories ORDER BY name LIMIT 2;")
    assert rc == 0, err
    assert out.splitlines() == ["name\ttier", "dup_one\tinstance", "dup_two\tinstance"]


def test_sql_tsv_flattens_newlines_and_null(db):
    rc, out, _ = _cli(db, "sql", "SELECT text, NULL AS n FROM memories WHERE name='exempt_row'")
    lines = out.splitlines()
    assert len(lines) == 2 and lines[0] == "text\tn"
    assert lines[1].startswith("<!-- budget-exempt --> dword0") and lines[1].endswith("\t")


def test_sql_json_and_with(db):
    rc, out, err = _cli(
        db, "sql", "--json",
        "WITH t AS (SELECT tier, COUNT(*) AS n FROM memories GROUP BY tier) "
        "SELECT * FROM t ORDER BY tier",
    )
    assert rc == 0, err
    assert json.loads(out) == [
        {"tier": "class", "n": 1}, {"tier": "instance", "n": 2},
        {"tier": "shared-global", "n": 1}, {"tier": "shared-projects", "n": 1},
    ]


def test_sql_does_not_load_vec_extension(db):
    # La tabella vec0 non e' interrogabile senza estensione: errore SQL, non un crash.
    rc, out, err = _cli(db, "sql", "SELECT COUNT(*) FROM memories_vec")
    assert rc == 1 and "vec0" in err
    assert mdb.run_sql("SELECT COUNT(*) FROM memories_fts_docsize", db)[1] == [(5,)]


@pytest.mark.parametrize("stmt", [
    "SELECT 1; SELECT 2",
    "SELECT 1; DELETE FROM memories",
    "PRAGMA table_info(memories)",
    "PRAGMA journal_mode=WAL",
    "ATTACH DATABASE 'x.db' AS x",
    "DELETE FROM memories",
    "INSERT INTO links VALUES (1, 'x')",
    "UPDATE memories SET tier='x'",
    "DROP TABLE memories",
    "VACUUM",
    "",
    " ; ",
    "-- comment\nSELECT 1",
])
def test_sql_refusals_exit_2(rwdb, stmt):
    rc, out, err = _cli(rwdb, "sql", stmt)
    assert rc == 2, (out, err)
    assert out == "" and "refused" in err
    assert mdb.run_sql("SELECT COUNT(*) FROM memories", rwdb)[1] == [(1,)]


@pytest.mark.parametrize("stmt", [
    "WITH x AS (SELECT 1) DELETE FROM memories",
    "SELECT * FROM pragma_table_info('memories')",
])
def test_sql_engine_level_denials(rwdb, stmt):
    # Superano il filtro testuale ma li ferma SQLite (DB read-only + authorizer).
    rc, out, err = _cli(rwdb, "sql", stmt)
    assert rc == 1 and out == ""
    assert mdb.run_sql("SELECT COUNT(*) FROM memories", rwdb)[1] == [(1,)]


def test_sql_missing_db_is_not_created(tmp_path):
    # Cambio di spec voluto (F4): un --db esplicito mancante esce con 3 come per gli altri comandi
    missing = tmp_path / "nope.db"
    rc, _, err = _cli(missing, "sql", "SELECT 1")
    assert rc == 3 and "DB not found" in err
    assert not missing.exists()


# --- stats -------------------------------------------------------------------


def test_stats_enriched(db):
    rc, out, err = _cli(db, "stats")
    assert rc == 0, err
    assert out.splitlines() == [
        "memories : 5",
        "fts rows : 5",
        "vec rows : 5",
        "tiers    : instance=2, class=1, shared-global=1, shared-projects=1",
        "types    : feedback=2, project=2, reference=1",
        f"updated  : {_NEW}",
    ]


def test_stats_empty(empty_db):
    rc, out, _ = _cli(empty_db, "stats")
    assert rc == 0
    assert out.splitlines() == [
        "memories : 0", "fts rows : 0", "vec rows : 0",
        "tiers    : -", "types    : -", "updated  : -",
    ]


# --- espansione di ~ ---------------------------------------------------------


def _home_env(home):
    # Path.expanduser usa USERPROFILE su Windows e HOME altrove.
    return {"USERPROFILE": str(home), "HOME": str(home)}


def test_tilde_expansion_on_db(tmp_path):
    rc, out, err = _run(_MEMDB, "--db", "~/sub.db", "init", env=_home_env(tmp_path))
    assert rc == 0, err
    assert (tmp_path / "sub.db").is_file()
    rc, out, err = _run(_MEMDB, "--db", "~/sub.db", "sql", "SELECT COUNT(*) AS n FROM memories",
                        env=_home_env(tmp_path))
    assert rc == 0 and out.splitlines() == ["n", "0"]
    rc, out, err = _run(_MEMDB, "compact", "--db", "~/sub.db", env=_home_env(tmp_path))
    assert rc == 0, err


def test_global_db_is_honoured_by_compact(tmp_path):
    # Regressione: il --db del sottoparser `compact` sovrascriveva quello globale.
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "copy.db"
    mdb.init_db(target)
    rc, out, err = _run(_MEMDB, "--db", str(target), "compact", env=_home_env(home))
    assert rc == 0, err
    assert not (home / ".claude" / "agent-memory" / ".memory.db").exists()


def test_tilde_expansion_on_text_file(tmp_path):
    db = tmp_path / "t.db"
    mdb.init_db(db)
    rc, _, err = _cli(db, "upsert", "--tier", "instance", "--type", "user", "--name", "n",
                      "--description", "d", "--text-file", "~/missing_body.md",
                      env=_home_env(tmp_path))
    assert rc == 1
    assert str(tmp_path / "missing_body.md") in err and "~" not in err.split("not found:")[1]


# --- scan_mem_matrix ---------------------------------------------------------

_ENV = {"MEM_NOW": _NOW, "MEM_OVERSIZE_BYTES": "200"}


def _scan(db, *args, env=None):
    return _run(_SCAN, *args, env=dict(_ENV, MEMORY_DB_PATH=str(db), **(env or {})))


def _health(db, *args, env=None):
    return _run(_HEALTH, *args, env=dict(_ENV, MEMORY_DB_PATH=str(db), **(env or {})))


def test_scan_table_empty_db(empty_db):
    rc, out, _ = _scan(empty_db, "--budgets")
    assert rc == 0
    assert out == (
        "TIER\tROWS\tBYTES\tBIGGEST\tFAT(>200)\tSTALE(>60d)\n"
        "TOTAL\t0\t0\t0\t0\t0\n"
        "INTEGRITY\trows=0\tfts_idx=0\tvec=present\tOK\t-\n"
    )


def test_scan_table_populated(db):
    rc, out, _ = _scan(db)  # nessun argomento == --budgets
    assert rc == 0
    dup_bytes = len(_DUP_TEXT) + len(_DUP_TEXT + " again")
    total = dup_bytes + len(_FAT_TEXT) + len(_EXEMPT_TEXT) + len(_CLASS_TEXT)
    biggest = max(len(_FAT_TEXT), len(_EXEMPT_TEXT))
    assert out.splitlines() == [
        "TIER\tROWS\tBYTES\tBIGGEST\tFAT(>200)\tSTALE(>60d)",
        f"instance\t2\t{dup_bytes}\t{len(_DUP_TEXT) + 6}\t0\t2",
        f"class\t1\t{len(_CLASS_TEXT)}\t{len(_CLASS_TEXT)}\t0\t0",
        f"shared-global\t1\t{len(_EXEMPT_TEXT)}\t{len(_EXEMPT_TEXT)}\t1\t0",
        f"shared-projects\t1\t{len(_FAT_TEXT)}\t{len(_FAT_TEXT)}\t1\t0",
        f"TOTAL\t5\t{total}\t{biggest}\t2\t2",
        "INTEGRITY\trows=5\tfts_idx=5\tvec=present\tOK\t-",
    ]
    assert _scan(db, "--budgets")[1] == out


def test_scan_json(db, empty_db):
    rc, out, _ = _scan(db, "--json")
    doc = json.loads(out)
    assert rc == 0 and not out.endswith("\n") and ", " not in out
    assert doc["available"] is True
    assert doc["oversize_bytes"] == 200 and doc["stale_days"] == 60
    assert doc["integrity"] == {"rows": 5, "fts_index": 5, "vec_present": 1, "cohesive": True}
    assert doc["total"]["rows"] == 5
    assert [t["tier"] for t in doc["tiers"]] == [
        "instance", "class", "shared-global", "shared-projects"]
    assert doc["tiers"][0] == {"tier": "instance", "rows": 2,
                               "bytes": 2 * len(_DUP_TEXT) + 6,
                               "biggest": len(_DUP_TEXT) + 6, "fat": 0, "stale": 2}
    empty = json.loads(_scan(empty_db, "--json")[1])
    assert empty["tiers"] == [] and empty["integrity"]["cohesive"] is True


def test_scan_env_overrides(db):
    rc, out, _ = _scan(db, env={"MEM_OVERSIZE_BYTES": "10", "MEM_STALE_DAYS": "5"})
    lines = out.splitlines()
    assert lines[0].endswith("FAT(>10)\tSTALE(>5d)")
    assert lines[-2].split("\t")[4:] == ["5", "5"]  # tutte oversize, tutte piu' vecchie di 5 giorni


def test_scan_missing_and_invalid_db(tmp_path):
    missing = tmp_path / "absent.db"
    rc, out, _ = _scan(missing)
    assert rc == 0
    assert out.splitlines()[1] == f"(skip: DB not built ({missing}))\t0\t0\t0\t0\t0"
    assert not missing.exists()
    doc = json.loads(_scan(missing, "--json")[1])
    assert doc == {"available": False, "reason": f"DB not built ({missing})", "tiers": []}

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a sqlite file, not even close" * 50)
    assert _scan(junk)[1].splitlines()[1].startswith("(skip: invalid SQLite)")


def test_scan_unknown_format_exits_1(db):
    rc, out, err = _scan(db, "--nope")
    assert rc == 1 and out == "" and "Unknown format: --nope" in err


def test_scan_is_read_only(db):
    before = os.path.getmtime(db), os.path.getsize(db)
    _scan(db)
    _health(db, "--verbose")
    assert (os.path.getmtime(db), os.path.getsize(db)) == before


# --- mem_health --------------------------------------------------------------


def test_health_empty_db(empty_db):
    rc, out, _ = _health(empty_db)
    lines = out.splitlines()
    assert rc == 0
    assert lines[0] == "## Memory Health Report (mode: normal)" and lines[1] == ""
    assert "| 1 | Right-sized rows | 0/20 | no rows in DB |" in lines
    assert "| 3 | No near-dup rows | 20/20 | no rows to compare (full) |" in lines
    assert "| 5 | Metadata complete | 0/15 | no rows |" in lines
    assert "| 6 | Staleness managed | 0/10 | no rows |" in lines
    assert "v3 Triggers: None. DB within thresholds." in lines
    assert lines[-2] == "Scored 35/100 pts (renormalized to /100)."
    assert lines[-1] == "SCORE: 35/100"


def test_health_populated_scored(db):
    rc, out, _ = _health(db)
    lines = out.splitlines()
    assert rc == 0
    assert lines[2:10] == [
        "| # | Criterion | Score | Detail |",
        "|---|-----------|-------|--------|",
        "| 1 | Right-sized rows | 16/20 | 4/5 within 200B; fat: fat_row; exempt: 1 |",
        "| 2 | FTS index cohesion | 20/20 | fts index aligned (rows=5, fts_idx=5) |",
        "| 3 | No near-dup rows | 16/20 | 1 near-dup pair(s) (Jaccard>=0.82) across 5 rows |",
        "| 4 | vec store soundness | 15/15 | memories_vec present (vec0); full triple via "
        "memory_db.py stats |",
        "| 5 | Metadata complete | 12/15 | 4/5 have name+description+type |",
        "| 6 | Staleness managed | 6/10 | 2/5 older than 60d (edit-span 139.0d) |",
    ]
    assert "- 1 oversized row(s) (>200B): consolidate via /lt-mem --compact (1 exempt, skipped)" \
        in lines
    assert "- 40% rows stale (>60d): run /lt-mem archive pass" in lines
    assert lines[-2] == "Scored 85/100 pts (renormalized to /100)."
    assert lines[-1] == "SCORE: 85/100"


def test_health_quick_skips_dup_scan(db):
    rc, out, _ = _health(db, "--quick")
    lines = out.splitlines()
    assert lines[0] == "## Memory Health Report (mode: quick)"
    assert any(l.startswith("| 3 | No near-dup rows | 20/20 | skipped (--quick)") for l in lines)
    assert lines[-1] == "SCORE: 89/100"


def test_health_verbose_appends_scan_table(db):
    rc, out, _ = _health(db, "--verbose")
    lines = out.splitlines()
    start = lines.index("```")
    end = lines.index("```", start + 1)
    assert lines[start - 1] == "### Per-tier DB signals (from scan_mem_matrix.py)"
    assert lines[start + 1:end] == _scan(db)[1].splitlines()
    assert lines[-1] == "SCORE: 85/100"


def test_health_na_renormalisation_and_cap(db):
    # Soglia 365 giorni > edit-span (139): criterio 6 N/A, punteggio su 90.
    rc, out, _ = _health(db, env={"MEM_STALE_DAYS": "365"})
    lines = out.splitlines()
    assert any(l.startswith("| 6 | Staleness managed | N/A | edit-span 139.0d < 365d") for l in lines)
    assert not any("rows stale" in l for l in lines)
    assert lines[-2].startswith("Scored 79/90 pts (renormalized to /100; 10 pts N/A excluded; "
                                "capped at 97")
    assert lines[-1] == "SCORE: 88/100"  # 79/90 = 87.8 -> 88 (half-up)

    # Soglie permissive: 87/90; il tetto MEM_INCOMPLETE_CAP si applica solo se superato.
    rc, out, _ = _health(db, "--quick", env={"MEM_STALE_DAYS": "365",
                                             "MEM_OVERSIZE_BYTES": "100000"})
    assert out.splitlines()[-1] == "SCORE: 97/100"  # 87/90 = 96.7 -> 97
    rc, out, _ = _health(db, "--quick", env={"MEM_STALE_DAYS": "365",
                                             "MEM_OVERSIZE_BYTES": "100000",
                                             "MEM_INCOMPLETE_CAP": "50"})
    assert out.splitlines()[-1] == "SCORE: 50/100"


def test_health_dup_threshold_and_corpus_trigger(db):
    rc, out, _ = _health(db, env={"MEM_DUP_SIM_MIN": "0.99", "MEM_CORPUS_BYTES_MAX": "100"})
    lines = out.splitlines()
    assert any("| 3 | No near-dup rows | 20/20 | 0 near-dup pair(s) (Jaccard>=0.99)" in l
               for l in lines)
    assert any(l.startswith("- Corpus ") and l.endswith("(>100B): run /lt-mem --quick all")
               for l in lines)


def test_health_missing_db_scores_zero(tmp_path):
    missing = tmp_path / "absent.db"
    rc, out, _ = _health(missing)
    lines = out.splitlines()
    assert rc == 0
    assert lines[2].startswith(f"memory DB unavailable: memory DB not built ({missing})")
    assert f"| 6 | Staleness managed | 0/10 | memory DB not built ({missing}) |" in lines
    assert lines[-1] == "SCORE: 0/100"
    assert not missing.exists()


def test_health_claude_dir_fallback(tmp_path, empty_db):
    target = tmp_path / "agent-memory"
    target.mkdir()
    (target / ".memory.db").write_bytes(open(empty_db, "rb").read())
    rc, out, _ = _run(_HEALTH, env={"CLAUDE_DIR": str(tmp_path), "MEM_NOW": _NOW})
    assert out.splitlines()[-1] == "SCORE: 35/100"


def test_near_dup_pair_counter_matches_bash_tokenisation():
    count = mem_health.count_near_dup_pairs
    assert count(["aaa bbb ccc ddd", "AAA, bbb; ccc|ddd"], 0.82) == 1   # solo ASCII, separatori
    assert count(["aaa bbb ccc ddd", "aaa bbb ccc eee"], 0.82) == 0     # 3/5 = 0.6
    assert count(["ab cd", "ab cd"], 0.5) == 0                          # token < 3 ignorati
    assert count(["aaa bbb", "aaa bbb", "aaa bbb"], 0.82) == 3          # coppie, non righe
    assert mem_health.pct_points(4, 5, 20) == 16 and mem_health.pct_points(1, 0, 20) == 0


def test_scan_module_api_with_explicit_path(empty_db):
    assert scan.table_lines(empty_db)[-1] == "INTEGRITY\trows=0\tfts_idx=0\tvec=present\tOK\t-"


def test_init_creates_missing_parent_directory(tmp_path):
    """Prima installazione: la cartella del DB non esiste ancora e va creata."""
    import subprocess
    from pathlib import Path
    import sys
    db = tmp_path / "fresh" / "nested" / ".memory.db"
    script = Path(__file__).resolve().parent / "memory_db.py"
    res = subprocess.run([sys.executable, str(script), "--db", str(db), "init"],
                         capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    assert db.is_file()


# --- name ties live+archive, --db mancante -----------------------------------


def _tie_db(tmp_path):
    """DB con un gemello vivo e uno archiviato con lo stesso name (id vivo < id archiviato)."""
    p = tmp_path / "tie.db"
    mdb.init_db(p)
    live = mdb.upsert(path="instance/_/tiedup.md", tier="instance", agent=None, type="feedback",
                      name="tiedup", description="live twin", text="live twin body", db_path=p)
    arch = mdb.upsert(path="instance/x/tiedup.md", tier="instance", agent=None, type="feedback",
                      name="tiedup", description="archived twin", text="archived twin body", db_path=p)
    mdb.archive(arch, db_path=p)
    return p, live, arch


def _count(db):
    return mdb.run_sql("SELECT COUNT(*) FROM memories", db)[1][0][0]


def test_get_name_tie_prefers_live_row(tmp_path):
    p, live, _ = _tie_db(tmp_path)
    rc, out, err = _cli(p, "get", "--name", "tiedup")
    assert rc == 0, err
    assert out.strip() == "live twin body"


def test_similar_name_tie_prefers_live_row(tmp_path):
    p, live, _ = _tie_db(tmp_path)
    rc, out, err = _cli(p, "similar", "--name", "tiedup")
    assert rc == 0, err
    assert f"similar to [{live}]" in out


def test_get_prefix_tie_prefers_live_row(tmp_path):
    p = tmp_path / "pfx.db"
    mdb.init_db(p)
    mdb.upsert(path="instance/_/pfxtie_one.md", tier="instance", agent=None, type="feedback",
               name="pfxtie_one", description="d", text="live prefix body", db_path=p)
    arch = mdb.upsert(path="instance/_/pfxtie_two.md", tier="instance", agent=None,
                      type="feedback", name="pfxtie_two", description="d",
                      text="archived prefix body", db_path=p)
    mdb.archive(arch, db_path=p)
    rc, out, err = _cli(p, "get", "--name", "pfxtie")
    assert rc == 0, err
    assert out.strip() == "live prefix body"


def test_get_name_two_live_rows_exits_3_with_suggestions(tmp_path):
    p = tmp_path / "two.db"
    mdb.init_db(p)
    for sub in ("a", "b"):
        mdb.upsert(path=f"instance/{sub}/twolive.md", tier="instance", agent=None,
                   type="feedback", name="twolive", description="d", text=f"body {sub}",
                   db_path=p)
    rc, out, err = _cli(p, "get", "--name", "twolive")
    assert rc == 3
    assert "did you mean" in err


@pytest.mark.parametrize("cmd", [("prune",), ("archive",), ("retier", "--tier", "class")])
def test_destructive_name_tie_exits_3_and_changes_nothing(tmp_path, cmd):
    p, live, arch = _tie_db(tmp_path)
    before = mdb.run_sql("SELECT id, tier, path FROM memories ORDER BY id", p)[1]
    rc, out, err = _cli(p, cmd[0], "--name", "tiedup", *cmd[1:])
    assert rc == 3, err
    assert str(live) in err and str(arch) in err  # id candidati per rilanciare con --id
    assert _count(p) == 2
    assert mdb.run_sql("SELECT id, tier, path FROM memories ORDER BY id", p)[1] == before


_DESTRUCTIVE_CMDS = [("prune",), ("archive",), ("retier", "--tier", "class")]


def _name_db(tmp_path, specs, archived=()):
    """DB con le righe `specs` = (path, name) in ordine di id; `archived` = indici da archiviare."""
    p = tmp_path / "names.db"
    mdb.init_db(p)
    ids = [mdb.upsert(path=path, tier="instance", agent=None, type="feedback", name=name,
                      description="d", text=f"body of {path}", db_path=p)
           for path, name in specs]
    for index in archived:
        mdb.archive(ids[index], db_path=p)
    return p, ids


def _rows_state(db):
    return mdb.run_sql("SELECT id, tier, path, text FROM memories ORDER BY id", db)[1]


def _assert_tie_refused(p, ids, rc, err, before):
    assert rc == 3, err
    for rid in ids:
        assert f"id={rid} " in err  # ogni riga a pareggio compare con il suo id
    assert _rows_state(p) == before


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_name_never_resolves_and_leaves_rows_untouched(tmp_path, blank):
    # N1: un --name vuoto o di soli spazi non deve risolvere nemmeno una riga con name vuoto
    p, ids = _name_db(tmp_path, [("instance/a/blank.md", ""), ("instance/a/other.md", "other")])
    before = _rows_state(p)
    for cmd in (("prune",), ("get",)):
        rc, _, err = _cli(p, cmd[0], "--name", blank)
        assert rc == 3, err
    assert _rows_state(p) == before


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_CMDS)
def test_destructive_tie_on_same_name_different_stems_exits_3(tmp_path, cmd):
    # F1/S1: due righe vive con lo stesso name e stem diversi (suffisso --wsl del merge)
    p, ids = _name_db(tmp_path, [("instance/a/foo-notes.md", "foo-notes"),
                                 ("instance/a/foo-notes--wsl.md", "foo-notes")])
    before = _rows_state(p)
    rc, _, err = _cli(p, cmd[0], "--name", "foo-notes", *cmd[1:])
    _assert_tie_refused(p, ids, rc, err, before)


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_CMDS)
def test_destructive_tie_live_plus_archived_different_stems_exits_3(tmp_path, cmd):
    # F1/S2: una viva e una archiviata con lo stesso name e stem diversi (copia di conflitto)
    p, ids = _name_db(tmp_path, [("instance/a/ctx.md", "ctx"), ("instance/a/ctx-old.md", "ctx")],
                      archived=[1])
    before = _rows_state(p)
    rc, _, err = _cli(p, cmd[0], "--name", "ctx", *cmd[1:])
    _assert_tie_refused(p, ids, rc, err, before)


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_CMDS)
def test_destructive_tie_on_slug_exits_3(tmp_path, cmd):
    # passo 2: stesso stem, nomi diversi, nessuna riga col name esatto
    p, ids = _name_db(tmp_path, [("instance/a/shared-stem.md", "first title"),
                                 ("instance/b/shared-stem.md", "second title")])
    before = _rows_state(p)
    rc, _, err = _cli(p, cmd[0], "--name", "shared-stem", *cmd[1:])
    _assert_tie_refused(p, ids, rc, err, before)


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_CMDS)
def test_destructive_prefix_tie_exits_3_and_does_not_fall_to_fts(tmp_path, cmd):
    p, ids = _name_db(tmp_path, [("instance/a/pfx-one.md", "pfx-one"),
                                 ("instance/a/pfx-two.md", "pfx-two")])
    before = _rows_state(p)
    rc, _, err = _cli(p, cmd[0], "--name", "pfx", *cmd[1:])
    _assert_tie_refused(p, ids, rc, err, before)


@pytest.mark.parametrize("cmd", [("get",), ("similar",)])
def test_read_two_live_rows_different_stems_exits_3(tmp_path, cmd):
    p, ids = _name_db(tmp_path, [("instance/a/foo-notes.md", "foo-notes"),
                                 ("instance/a/foo-notes--wsl.md", "foo-notes")])
    rc, out, err = _cli(p, cmd[0], "--name", "foo-notes")
    assert rc == 3, err
    assert out == ""
    for rid in ids:
        assert f"id={rid} " in err


@pytest.mark.parametrize("cmd", [("get",), ("similar",)])
def test_read_all_archived_exact_tie_does_not_redirect_to_live_prefix(tmp_path, cmd):
    # F6: due archiviate col name esatto + una viva che lo ha come prefisso
    p, ids = _name_db(tmp_path, [("instance/p/deploy-notes.md", "deploy-notes"),
                                 ("instance/q/deploy-notes.md", "deploy-notes"),
                                 ("instance/p/deploy-notes-v2.md", "deploy-notes-v2")],
                      archived=[0, 1])
    rc, out, err = _cli(p, cmd[0], "--name", "deploy-notes")
    assert rc == 3, err
    assert "UNRELATED" not in out and "body of instance/p/deploy-notes-v2.md" not in out
    assert f"id={ids[0]} " in err and f"id={ids[1]} " in err


def test_get_live_plus_archived_different_stems_resolves_the_live_row(tmp_path):
    p, ids = _name_db(tmp_path, [("instance/a/ctx-old.md", "ctx"), ("instance/a/ctx.md", "ctx")],
                      archived=[0])
    rc, out, err = _cli(p, "get", "--name", "ctx")
    assert rc == 0, err
    assert out.strip() == "body of instance/a/ctx.md"


def test_tie_group_larger_than_suggestion_limit_lists_every_id(tmp_path):
    # F7: 8 righe a pareggio, limite di default 5: tutti gli id vanno stampati
    specs = [(f"instance/a/big-{i}.md", "big") for i in range(8)]
    p, ids = _name_db(tmp_path, specs)
    before = _rows_state(p)
    rc, _, err = _cli(p, "prune", "--name", "big")
    _assert_tie_refused(p, ids, rc, err, before)


def test_resolve_name_tied_rows_come_before_other_suggestions(tmp_path):
    specs = [(f"instance/a/zz-{i}.md", "zz") for i in range(7)] + [("instance/a/zz-other.md", "zz-other")]
    p, ids = _name_db(tmp_path, specs)
    conn = mdb._connect(p)
    try:
        rid, suggestions = mdb._resolve_name(conn, "zz")
    finally:
        conn.close()
    assert rid is None
    assert [s["id"] for s in suggestions[:7]] == ids[:7]


@pytest.mark.parametrize("cmd", [("get", "--id", "1"), ("search", "x"), ("stats",),
                                 ("list",), ("compact",)])
def test_missing_db_exits_3_and_creates_nothing(tmp_path, cmd):
    missing = tmp_path / "new_parent" / "nope.db"
    rc, _, err = _cli(missing, *cmd)
    assert rc == 3, err
    assert not missing.exists()
    assert not missing.parent.exists()


@pytest.mark.parametrize("empty", ["", "   "])
@pytest.mark.parametrize("cmd", [("prune", "--name", "victim"), ("stats",), ("init",)])
def test_empty_db_argument_exits_3_and_never_uses_default_db(tmp_path, cmd, empty):
    # F5: --db "" (variabile di shell non impostata) non deve ricadere sul DB di default.
    # HOME finta: anche se il guard mancasse, il DB reale non verrebbe toccato.
    home = tmp_path / "fakehome"
    (home / ".claude" / "agent-memory").mkdir(parents=True)
    env = {"USERPROFILE": str(home), "HOME": str(home)}
    default_db = home / ".claude" / "agent-memory" / ".memory.db"
    rc, out, err = _run(_MEMDB, "--db", empty, *cmd, env=env)
    assert rc == 3, (out, err)
    assert "empty --db" in err
    assert not default_db.exists()


def test_init_on_missing_db_still_creates(tmp_path):
    fresh = tmp_path / "new_parent" / "fresh.db"
    rc, _, err = _cli(fresh, "init")
    assert rc == 0, err
    assert fresh.is_file()


# --- timestamp, regola di bump, realm, API batch ------------------------------

_T_OLD = "2000-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _no_ambient_realm(monkeypatch):
    """Un SC_MEM_REALM ereditato dalla sessione non deve cambiare l'esito dei test."""
    monkeypatch.delenv("SC_MEM_REALM", raising=False)


def _new_db(tmp_path, name="t12.db"):
    p = tmp_path / name
    mdb.init_db(p)
    return p


def _put(db, name, path=None, **kw):
    """upsert di una riga con valori di default; `kw` passa direttamente a mdb.upsert."""
    args = dict(tier="instance", agent=None, type="feedback", description="desc",
                text=f"body of {name}")
    args.update(kw)
    return mdb.upsert(path=path or f"instance/_/{name}.md", name=name, db_path=db, **args)


def _fetch(db, path):
    cols, rows = mdb.run_sql(f"SELECT * FROM memories WHERE path = '{path}'", db)
    return dict(zip(cols, rows[0])) if rows else None


def _vec_bytes(db, row_id):
    conn = mdb._connect(db)
    try:
        row = conn.execute("SELECT embedding FROM memories_vec WHERE rowid = ?",
                           (row_id,)).fetchone()
        return None if row is None else bytes(row[0])
    finally:
        conn.close()


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _columns(db):
    conn = sqlite3.connect(str(db))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    finally:
        conn.close()


def _spy_embed(monkeypatch):
    """Registra ogni chiamata al modello (lista di testi) lasciando passare l'embedding reale."""
    calls = []
    real = mdb._embed_batch

    def spy(texts):
        calls.append(list(texts))
        return real(texts)

    monkeypatch.setattr(mdb, "_embed_batch", spy)
    return calls


# timestamp ------------------------------------------------------------------


def test_upsert_stores_iso_offset_timestamps_verbatim(tmp_path):
    p = _new_db(tmp_path)
    created, updated = "2026-10-15T17:30:00+00:00", "2026-10-16T08:00:00.5+00:00"
    _put(p, "ts", created=created, updated=updated)
    row = _fetch(p, "instance/_/ts.md")
    assert (row["created"], row["updated"]) == (created, updated)


@pytest.mark.parametrize("offset", ["+02:00", "-05:00", "+00:30"])
def test_upsert_rejects_non_utc_offset(tmp_path, offset):
    # F3: con un offset diverso le stringhe non si ordinano come gli istanti
    p = _new_db(tmp_path)
    with pytest.raises(ValueError):
        _put(p, "off", updated=f"2026-10-16T08:00:00{offset}")
    assert _count(p) == 0


@pytest.mark.parametrize("field", ["created", "updated"])
@pytest.mark.parametrize("bad", [
    "2026-10-15T17:30:00",          # naive
    "2026-10-15",                   # solo data
    "yesterday",                    # non ISO
    "",                             # vuoto
    "2026-13-45T00:00:00+00:00",    # calendario impossibile
    "2026-10-15T17:30:00Z",         # Z: l'ordinamento a stringa non e' piu' coerente con +00:00
    "2026-10-15 17:30:00+00:00",    # separatore diverso da T
    " 2026-10-15T17:30:00+00:00",   # spazio iniziale
    "2026-10-15T17:30:00+0000",     # offset senza due punti
    20261015,                       # non stringa
])
def test_upsert_rejects_naive_or_malformed_timestamp(tmp_path, field, bad):
    p = _new_db(tmp_path)
    with pytest.raises(ValueError):
        _put(p, "bad", **{field: bad})
    assert _count(p) == 0


def test_rejected_timestamp_on_update_leaves_row_untouched(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "keep", created=_T_OLD, updated=_T_OLD)
    before = _sha(p)
    with pytest.raises(ValueError):
        _put(p, "keep", text="changed", updated="2026-10-15T17:30:00")
    assert _sha(p) == before


def test_upsert_without_timestamps_uses_now(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "now")
    row = _fetch(p, "instance/_/now.md")
    assert row["created"] == row["updated"]
    assert row["created"].endswith("+00:00") and row["created"] > "2026-01-01"


# regola di bump ------------------------------------------------------------------

_BUMP_CASES = [
    ("tier", {"tier": "class"}),
    ("agent", {"agent": "w-x"}),
    ("type", {"type": "project"}),
    ("name", {"name": "renamed"}),
    ("description", {"description": "another description"}),
    ("text", {"text": "another body"}),
    ("realm", {"realm": "personal"}),
]


@pytest.mark.parametrize("field,change", _BUMP_CASES, ids=[c[0] for c in _BUMP_CASES])
def test_update_bumps_updated_when_a_field_changes(tmp_path, field, change):
    p = _new_db(tmp_path)
    base = dict(path="instance/_/bump.md", tier="instance", agent=None, type="feedback",
                name="bump", description="desc", text="body", db_path=p)
    mdb.upsert(**base, created=_T_OLD, updated=_T_OLD)
    mdb.upsert(**{**base, **change})
    row = _fetch(p, "instance/_/bump.md")
    assert row["updated"] > _T_OLD
    assert row["created"] == _T_OLD


def test_noop_upsert_writes_nothing_and_does_not_embed(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    _put(p, "same", created=_T_OLD, updated=_T_OLD, realm="personal")
    before = _sha(p)
    calls = _spy_embed(monkeypatch)
    _put(p, "same", realm="personal")
    _put(p, "same")  # realm omesso: la riga esistente lo conserva, quindi nessuna modifica
    assert _sha(p) == before  # nessuna scrittura: il file e' identico byte per byte
    assert _fetch(p, "instance/_/same.md")["updated"] == _T_OLD
    assert calls == []


def test_noop_upsert_many_issues_no_write(tmp_path):
    p = _new_db(tmp_path)
    row = _mrow("same", created=_T_OLD, updated=_T_OLD)
    conn = mdb._connect(p)
    try:
        conn.execute("BEGIN IMMEDIATE")
        mdb.upsert_many([row], conn)
        conn.commit()
        changes = conn.total_changes
        conn.execute("BEGIN IMMEDIATE")
        assert mdb.upsert_many([dict(row)], conn) == [_fetch(p, row["path"])["id"]]
        assert conn.total_changes == changes
        conn.execute("ROLLBACK")  # il guard F2 impone un BEGIN esplicito del chiamante
    finally:
        conn.close()


def test_explicit_timestamps_on_update_are_stored_as_given(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "x", created=_T_OLD, updated=_T_OLD)
    stamp = "2001-02-03T04:05:06+00:00"
    _put(p, "x", text="changed", updated=stamp)
    assert _fetch(p, "instance/_/x.md")["updated"] == stamp
    # nessun campo cambia, ma il timestamp esplicito e' diverso: va scritto cosi' com'e'
    later = "2002-02-03T04:05:06+00:00"
    _put(p, "x", text="changed", updated=later, created="1999-01-01T00:00:00+00:00")
    row = _fetch(p, "instance/_/x.md")
    assert (row["updated"], row["created"]) == (later, "1999-01-01T00:00:00+00:00")


def test_metadata_only_change_keeps_the_vector(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    rid = _put(p, "meta", created=_T_OLD, updated=_T_OLD)
    vec = _vec_bytes(p, rid)
    calls = _spy_embed(monkeypatch)
    _put(p, "meta", tier="class", agent="w-x", type="project", realm="personal")
    assert calls == []
    assert _vec_bytes(p, rid) == vec
    assert _fetch(p, "instance/_/meta.md")["updated"] > _T_OLD


def test_text_change_reembeds(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    rid = _put(p, "txt")
    vec = _vec_bytes(p, rid)
    calls = _spy_embed(monkeypatch)
    _put(p, "txt", text="completely different words about turbines")
    assert calls == [["completely different words about turbines"]]
    assert _vec_bytes(p, rid) != vec


# realm ------------------------------------------------------------------------


@pytest.mark.parametrize("env,expected", [
    ("personal", "personal"), (" Personal ", "personal"),
    (None, None), ("", None), ("work", None),
])
def test_new_row_realm_defaults_from_env(tmp_path, monkeypatch, env, expected):
    if env is not None:
        monkeypatch.setenv("SC_MEM_REALM", env)
    p = _new_db(tmp_path)
    _put(p, "r")
    assert _fetch(p, "instance/_/r.md")["realm"] == expected


def test_existing_row_keeps_realm_when_omitted(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    monkeypatch.setenv("SC_MEM_REALM", "personal")
    _put(p, "keepp")
    _put(p, "keepu", realm="untagged")
    monkeypatch.delenv("SC_MEM_REALM")
    _put(p, "keepp", text="changed p")
    monkeypatch.setenv("SC_MEM_REALM", "personal")
    _put(p, "keepu", text="changed u")
    assert _fetch(p, "instance/_/keepp.md")["realm"] == "personal"
    assert _fetch(p, "instance/_/keepu.md")["realm"] is None


@pytest.mark.parametrize("clear", ["untagged", None, " Untagged "])
def test_explicit_untagged_or_none_clears_realm(tmp_path, monkeypatch, clear):
    monkeypatch.setenv("SC_MEM_REALM", "personal")
    p = _new_db(tmp_path)
    _put(p, "clr", created=_T_OLD, updated=_T_OLD)
    assert _fetch(p, "instance/_/clr.md")["realm"] == "personal"
    _put(p, "clr", realm=clear)
    row = _fetch(p, "instance/_/clr.md")
    assert row["realm"] is None and row["updated"] > _T_OLD
    _put(p, "new_untagged", realm=clear)  # il valore esplicito batte l'env anche su riga nuova
    assert _fetch(p, "instance/_/new_untagged.md")["realm"] is None


@pytest.mark.parametrize("bad", ["work", "all", "", 5])
def test_upsert_rejects_unknown_realm(tmp_path, bad):
    p = _new_db(tmp_path)
    with pytest.raises(ValueError, match="realm"):
        _put(p, "badrealm", realm=bad)
    assert _count(p) == 0


def test_cli_upsert_realm_flag(tmp_path):
    p = _new_db(tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("realm cli body", encoding="utf-8")
    args = ["upsert", "--tier", "instance", "--type", "feedback", "--name", "cli-r",
            "--description", "d", "--text-file", str(body)]
    path = "instance/_/cli-r.md"
    rc, _, err = _cli(p, *args, "--realm", "personal")
    assert rc == 0, err
    assert _fetch(p, path)["realm"] == "personal"
    rc, _, err = _cli(p, *args)  # omesso: la riga esistente conserva il realm
    assert rc == 0, err
    assert _fetch(p, path)["realm"] == "personal"
    rc, _, err = _cli(p, *args, "--realm", "untagged")
    assert rc == 0, err
    assert _fetch(p, path)["realm"] is None
    rc, _, err = _cli(p, *args, "--realm", "work")
    assert rc == 2 and "--realm" in err


def test_cli_upsert_new_row_follows_env_realm(tmp_path):
    p = _new_db(tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("env realm body", encoding="utf-8")
    rc, _, err = _cli(p, "upsert", "--tier", "instance", "--type", "feedback", "--name", "envr",
                      "--description", "d", "--text-file", str(body),
                      env={"SC_MEM_REALM": "personal"})
    assert rc == 0, err
    assert _fetch(p, "instance/_/envr.md")["realm"] == "personal"


@pytest.fixture()
def realm_db(tmp_path):
    p = _new_db(tmp_path, "realm.db")
    _put(p, "pers", text="zebra quokka private note", realm="personal")
    _put(p, "open", text="zebra quokka public note")
    return p


def _names(hits):
    return sorted(h["name"] for h in hits)


def test_search_realm_filter(realm_db):
    def run(**kw):
        return _names(mdb.search("zebra quokka", k=10, mode="fts", db_path=realm_db, **kw))

    assert run() == ["open", "pers"]
    assert run(realm="all") == ["open", "pers"]
    assert run(realm="personal") == ["pers"]
    assert run(realm="untagged") == ["open"]
    many = mdb.search_many(["zebra", "quokka"], k=10, mode="fts", db_path=realm_db,
                           realm="personal")
    assert _names(many) == ["pers"]
    with pytest.raises(ValueError, match="realm"):
        mdb.search("zebra", db_path=realm_db, realm="work")


def test_cli_search_and_list_realm_filter(realm_db):
    def search(*extra):
        rc, out, err = _cli(realm_db, "search", "zebra quokka", "--mode", "fts", "--json", *extra)
        assert rc == 0, err
        return _names(json.loads(out))

    assert search() == ["open", "pers"]
    assert search("--realm", "personal") == ["pers"]
    assert search("--realm", "untagged") == ["open"]

    def listed(*extra):
        rc, out, err = _cli(realm_db, "list", *extra)
        assert rc == 0, err
        return sorted(line.split()[0] for line in out.splitlines() if line.strip())

    assert listed() == ["open", "pers"]
    assert listed("--realm", "all") == ["open", "pers"]
    assert listed("--realm", "personal") == ["pers"]
    assert listed("--realm", "untagged") == ["open"]
    rc, _, err = _cli(realm_db, "list", "--realm", "work")
    assert rc == 2


def test_get_and_similar_ignore_realm(realm_db):
    rc, out, err = _cli(realm_db, "get", "--name", "pers")
    assert rc == 0, err
    assert out.strip() == "zebra quokka private note"
    rc, out, err = _cli(realm_db, "similar", "--name", "pers")
    assert rc == 0, err
    assert "open" in out


# DB senza colonna realm, aperto in sola lettura ----------------------------------

_LEGACY_MEMORIES = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, tier TEXT, agent TEXT, type TEXT, name TEXT,
  description TEXT, text TEXT NOT NULL, html TEXT, created TEXT, updated TEXT, hash TEXT
);
"""
_LEGACY_ROWS = [
    ("legacy-one", "instance", "alpha legacy note about widgets"),
    ("legacy-two", "shared-global", "beta legacy note about sprockets"),
]


def _legacy_db(path):
    """DB con lo schema precedente a realm, popolato senza passare da upsert."""
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    mdb.sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_LEGACY_MEMORIES)
    conn.executescript(mdb._SCHEMA)  # IF NOT EXISTS: crea fts, vec e links, non tocca memories
    conn.executescript(mdb._TRIGGERS)
    for name, tier, text in _LEGACY_ROWS:
        cur = conn.execute(
            "INSERT INTO memories (path, tier, agent, type, name, description, text, created,"
            " updated, hash) VALUES (?, ?, NULL, 'feedback', ?, 'legacy', ?, ?, ?, ?)",
            (f"{tier}/_/{name}.md", tier, name, text, _T_OLD, _T_OLD,
             mdb._content_hash(text, name, "legacy")))
        conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                     (cur.lastrowid, mdb.sqlite_vec.serialize_float32(mdb._embed(text))))
    conn.commit()
    conn.close()
    return path


@contextlib.contextmanager
def _read_only_file(path):
    os.chmod(path, stat.S_IREAD)
    try:
        yield
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def test_columnless_readonly_db_still_serves_search_list_get_export(tmp_path):
    p = _legacy_db(tmp_path / "old.db")
    both = ["legacy-one", "legacy-two"]
    with _read_only_file(p):
        for realm, expected in [("all", both), ("untagged", both), ("personal", [])]:
            hits = mdb.search("legacy", k=10, mode="fts", db_path=p, realm=realm)
            assert _names(hits) == expected
        rc, out, err = _cli(p, "search", "legacy", "--mode", "fts", "--json",
                            "--realm", "personal")
        assert rc == 0, err
        assert json.loads(out) == []
        rc, out, err = _cli(p, "list", "--realm", "personal")
        assert rc == 0, err
        assert "(no memories match)" in out
        rc, out, err = _cli(p, "list", "--realm", "untagged")
        assert rc == 0, err
        assert all(name in out for name in both)
        rc, out, err = _cli(p, "get", "--name", "legacy-one")
        assert rc == 0, err
        assert out.strip() == "alpha legacy note about widgets"
        rc, out, err = _cli(p, "export")
        assert rc == 0, err
        exported = json.loads(out)
        assert [e["realm"] for e in exported] == [None, None]
        assert [e["created"] for e in exported] == [_T_OLD, _T_OLD]
    assert "realm" not in _columns(p)


# migrazione --------------------------------------------------------------------------


def test_fresh_schema_has_realm_column(tmp_path):
    assert _columns(_new_db(tmp_path))[-1] == "realm"


def test_migration_adds_column_once_and_is_idempotent(tmp_path):
    p = _legacy_db(tmp_path / "old.db")
    assert "realm" not in _columns(p)
    for _ in range(2):
        mdb._connect(p).close()
        assert _columns(p).count("realm") == 1
    mdb.init_db(p)
    assert _columns(p).count("realm") == 1
    assert _names(mdb.search("legacy", k=10, mode="fts", db_path=p)) == [
        "legacy-one", "legacy-two"]
    for path in ("instance/_/legacy-one.md", "shared-global/_/legacy-two.md"):
        assert _fetch(p, path)["realm"] is None  # righe esistenti: non taggate


def test_migration_tolerates_duplicate_column_race(tmp_path, monkeypatch):
    p = _legacy_db(tmp_path / "old.db")
    conn = mdb._connect(p)  # migra davvero
    try:
        # un altro processo ha aggiunto la colonna dopo il nostro controllo: il controllo e' vecchio
        monkeypatch.setattr(mdb, "_table_columns", lambda c, t: {"id", "path"})
        mdb._migrate_schema(conn)
    finally:
        conn.close()
    assert _columns(p).count("realm") == 1


def test_migration_skipped_while_another_writer_holds_the_lock(tmp_path, monkeypatch):
    p = _legacy_db(tmp_path / "locked.db")
    monkeypatch.setattr(mdb, "BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(str(p), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        mdb._connect(p).close()  # non solleva: la migrazione riprova alla connessione dopo
        assert "realm" not in _columns(p)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    mdb._connect(p).close()
    assert "realm" in _columns(p)


def test_migration_skipped_on_readonly_database_file(tmp_path):
    p = _legacy_db(tmp_path / "ro.db")
    with _read_only_file(p):
        mdb._connect(p).close()
        assert "realm" not in _columns(p)


def test_readonly_connection_never_migrates(tmp_path):
    p = _legacy_db(tmp_path / "old.db")
    conn = mdb._connect(p, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE memories SET tier = 'x'")
    finally:
        conn.close()
    assert "realm" not in _columns(p)


def test_migration_does_not_swallow_unexpected_errors(tmp_path):
    class FailingAlter:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("ALTER"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args)

    p = _legacy_db(tmp_path / "old.db")
    conn = sqlite3.connect(str(p))
    try:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            mdb._migrate_schema(FailingAlter(conn))
    finally:
        conn.close()


def test_personal_write_refused_when_realm_column_is_missing(tmp_path, monkeypatch):
    p = _legacy_db(tmp_path / "old.db")
    monkeypatch.setattr(mdb, "_migrate_schema", lambda conn: None)  # migrazione saltata
    with pytest.raises(RuntimeError, match="realm"):
        _put(p, "px", realm="personal")
    monkeypatch.setenv("SC_MEM_REALM", "personal")
    with pytest.raises(RuntimeError, match="realm"):
        _put(p, "py")
    monkeypatch.delenv("SC_MEM_REALM")
    assert _put(p, "pz")  # una riga senza realm si scrive anche senza colonna
    names = [r[0] for r in mdb.run_sql("SELECT name FROM memories ORDER BY name", p)[1]]
    assert names == ["legacy-one", "legacy-two", "pz"]


# upsert_many ----------------------------------------------------------------------


def _mrow(name, text=None, **extra):
    return {"path": f"instance/_/{name}.md", "tier": "instance", "agent": None,
            "type": "feedback", "name": name, "description": f"desc {name}",
            "text": text or f"body of {name}", **extra}


def _batch_conn(db):
    """Connessione con transazione governata dal chiamante, come nel caso d'uso del merge."""
    conn = mdb._connect(db)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _snapshot(db):
    """Stato logico completo: righe, vettori, link, e l'indice FTS verificato con integrity-check."""
    conn = mdb._connect(db)
    try:
        conn.execute("INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)")
        return {
            "memories": [tuple(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")],
            "vec": [(r[0], bytes(r[1])) for r in conn.execute(
                "SELECT rowid, embedding FROM memories_vec ORDER BY rowid")],
            "links": [tuple(r) for r in conn.execute(
                "SELECT src_id, dst_name FROM links ORDER BY src_id, dst_name")],
        }
    finally:
        conn.close()


def test_upsert_many_writes_rows_with_vectors_after_caller_commit(tmp_path):
    p = _new_db(tmp_path)
    rows = [_mrow("m1", created=_T_OLD, updated=_T_OLD, realm="personal"),
            _mrow("m2", text="see [[m1]] for details")]
    conn = _batch_conn(p)
    try:
        ids = mdb.upsert_many(rows, conn)
        assert len(ids) == 2 and ids[0] != ids[1]
        assert _count(p) == 0  # non ha committato: un altro lettore non vede nulla
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert _count(p) == 2
    first = _fetch(p, rows[0]["path"])
    assert (first["created"], first["updated"], first["realm"]) == (_T_OLD, _T_OLD, "personal")
    assert all(_vec_bytes(p, rid) for rid in ids)
    snap = _snapshot(p)
    assert (ids[1], "m1") in snap["links"]


def test_upsert_many_embeds_in_batches_before_any_write(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    conn = _batch_conn(p)
    seen = []
    real = mdb._embed_batch

    def spy(texts):
        seen.append((len(texts), conn.total_changes))
        return real(texts)

    monkeypatch.setattr(mdb, "_embed_batch", spy)
    monkeypatch.setattr(mdb, "EMBED_BATCH_SIZE", 2)
    try:
        start = conn.total_changes
        mdb.upsert_many([_mrow(f"b{i}") for i in range(5)], conn)
        assert conn.total_changes > start
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert [n for n, _ in seen] == [2, 2, 1]
    assert {changes for _, changes in seen} == {start}  # nessuna scrittura prima dei vettori


def test_upsert_many_embed_false_writes_no_vectors(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    calls = _spy_embed(monkeypatch)
    conn = _batch_conn(p)
    try:
        ids = mdb.upsert_many([_mrow("nv1"), _mrow("nv2")], conn, embed=False)
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert calls == []
    assert _count(p) == 2
    assert [_vec_bytes(p, rid) for rid in ids] == [None, None]
    assert _snapshot(p)["vec"] == []


def test_upsert_many_rollback_after_midbatch_failure_leaves_db_unchanged(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    _put(p, "ra", text="old body of ra [[target]]", created=_T_OLD, updated=_T_OLD)
    _put(p, "rb", text="untouched row")
    before = _snapshot(p)
    n_before = _count(p)

    real_links = mdb._refresh_links
    seen = {"n": 0}

    def flaky(conn, row_id, text):
        seen["n"] += 1
        if seen["n"] == 3:
            raise RuntimeError("boom")
        return real_links(conn, row_id, text)

    monkeypatch.setattr(mdb, "_refresh_links", flaky)
    batch = [_mrow("ra", text="new body of ra [[other]]"), _mrow("rc"), _mrow("rd"), _mrow("re")]
    conn = _batch_conn(p)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            mdb.upsert_many(batch, conn)
        # a meta' batch le scritture sono davvero avvenute dentro la transazione del chiamante
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] > n_before
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert _snapshot(p) == before
    assert _count(p) == n_before


def test_upsert_many_rollback_requested_after_success_also_restores_everything(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "ra", text="old body of ra", created=_T_OLD, updated=_T_OLD)
    before = _snapshot(p)
    conn = _batch_conn(p)
    try:
        mdb.upsert_many([_mrow("ra", text="new body"), _mrow("rn")], conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert _snapshot(p) == before


def test_upsert_many_applies_the_upsert_rules(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    monkeypatch.setenv("SC_MEM_REALM", "personal")
    conn = _batch_conn(p)
    try:
        mdb.upsert_many([_mrow("e1", created=_T_OLD, updated=_T_OLD), _mrow("e2", realm="untagged")],
                        conn)
        conn.execute("COMMIT")
        monkeypatch.delenv("SC_MEM_REALM")
        conn.execute("BEGIN IMMEDIATE")
        mdb.upsert_many([_mrow("e1", description="changed"), _mrow("e2")], conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    e1, e2 = _fetch(p, "instance/_/e1.md"), _fetch(p, "instance/_/e2.md")
    assert (e1["realm"], e2["realm"]) == ("personal", None)  # default da env solo su riga nuova
    assert e1["updated"] > _T_OLD and e1["description"] == "changed"
    assert e2["updated"] == e2["created"]  # e2 identico: nessun bump


@pytest.mark.parametrize("bad_row,match", [
    (_mrow("bad", tier="nonsense"), "tier"),
    (_mrow("bad", updated="2026-10-15T17:30:00"), "updated"),
    (_mrow("bad", realm="work"), "realm"),
    ({**_mrow("bad"), "nme": "typo"}, "nme"),
    ({k: v for k, v in _mrow("bad").items() if k != "text"}, "text"),
])
def test_upsert_many_validates_every_row_before_any_write(tmp_path, monkeypatch, bad_row, match):
    p = _new_db(tmp_path)
    calls = _spy_embed(monkeypatch)
    conn = _batch_conn(p)
    try:
        start = conn.total_changes
        with pytest.raises(ValueError, match=match):
            mdb.upsert_many([_mrow("good"), bad_row], conn)
        assert conn.total_changes == start
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert calls == []


def test_upsert_many_refuses_autocommit_connection_without_transaction(tmp_path):
    p = _new_db(tmp_path)
    conn = mdb._connect(p)
    conn.isolation_level = None  # ogni istruzione committerebbe da sola: batch non atomico
    try:
        with pytest.raises(RuntimeError, match="transaction"):
            mdb.upsert_many([_mrow("x")], conn)
    finally:
        conn.close()
    assert _count(p) == 0


def test_upsert_many_refuses_pep249_autocommit_connection_without_transaction(tmp_path):
    # F2: autocommit=True lascia isolation_level == '' ma ogni istruzione committa da sola
    p = _new_db(tmp_path)
    conn = sqlite3.connect(str(p), autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    mdb.sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        assert not conn.in_transaction
        with pytest.raises(RuntimeError, match="transaction"):
            mdb.upsert_many([_mrow("x")], conn, embed=False)
    finally:
        conn.close()
    assert _count(p) == 0


def test_upsert_many_refuses_default_connection_without_begin(tmp_path):
    p = _new_db(tmp_path)
    conn = mdb._connect(p)
    try:
        with pytest.raises(RuntimeError, match="transaction"):
            mdb.upsert_many([_mrow("x")], conn, embed=False)
    finally:
        conn.close()
    assert _count(p) == 0


def test_upsert_still_commits_each_row(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "c1")
    assert _count(p) == 1  # visibile a un altro lettore senza commit esterni


# export --------------------------------------------------------------------------------


def test_export_includes_realm_and_created(tmp_path):
    p = _new_db(tmp_path)
    created = "2026-10-15T17:30:00+00:00"
    _put(p, "ex1", created=created, updated=created, realm="personal")
    _put(p, "ex2", created=_T_OLD, updated=_T_OLD)
    for extra in ([], ["--with-body"]):
        rc, out, err = _cli(p, "export", *extra)
        assert rc == 0, err
        by_name = {e["name"]: e for e in json.loads(out)}
        assert (by_name["ex1"]["realm"], by_name["ex1"]["created"]) == ("personal", created)
        assert (by_name["ex2"]["realm"], by_name["ex2"]["created"]) == (None, _T_OLD)
        assert ("text" in by_name["ex1"]) == bool(extra)


# plan_index: il realm di default arriva dall'ambiente -----------------------------------


def _link_dir(link, target):
    # Le giunzioni NTFS non richiedono privilegi admin, a differenza dei symlink
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.parametrize("env_value,expected", [("personal", "personal"), (None, None)])
def test_plan_index_card_realm_follows_sc_mem_realm(tmp_path, monkeypatch, env_value, expected):
    home = tmp_path / "fakehome"
    (home / ".claude" / "agent-memory").mkdir(parents=True)
    (home / ".claude" / ".cache").mkdir()
    link = home / ".claude" / ".cache" / "fastembed"
    # il modello di embedding vive nel codice condiviso: la HOME finta lo raggiunge con una giunzione
    _link_dir(link, mdb.EMBED_CACHE_DIR)
    try:
        for var in ("USERPROFILE", "HOME"):
            monkeypatch.setenv(var, str(home))
        monkeypatch.delenv("SC_PROFILE", raising=False)
        if env_value is not None:
            monkeypatch.setenv("SC_MEM_REALM", env_value)
        db = home / ".claude" / "agent-memory" / ".memory.db"
        mdb.init_db(db)
        card_id = plan_index.upsert("t12-realm", "campaign: t12-realm\nstatus: DRAFT",
                                    "plan index probe")
        assert card_id.isdigit()
        rows = mdb.run_sql("SELECT name, realm FROM memories", db)[1]
        assert rows == [("plan-index-t12-realm", expected)]
    finally:
        # si toglie solo il collegamento: la pulizia della cartella temporanea non deve mai
        # arrivare alla cache reale del modello
        os.rmdir(link) if os.name == "nt" else os.unlink(link)
