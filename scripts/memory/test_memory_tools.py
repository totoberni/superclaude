"""Test per gli strumenti di memoria: alias dei tier, `sql`, `stats`, espansione di `~`
e i port Python scan_mem_matrix / mem_health.

Ogni test lavora su un DB temporaneo esplicito (`--db` o MEMORY_DB_PATH): il DB live
non viene mai aperto. Gli embedding sono reali (modello in cache, HF_HUB_OFFLINE=1).
I timestamp `updated` sono fissati a mano e MEM_NOW blocca "adesso", cosi' i
punteggi sono deterministici.
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import mem_health  # noqa: E402
import memory_db as mdb  # noqa: E402
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
                "MEMORY_DB_PATH", "CLAUDE_DIR"):
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
    missing = tmp_path / "nope.db"
    rc, _, err = _cli(missing, "sql", "SELECT 1")
    assert rc == 1 and "DB not found" in err
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
