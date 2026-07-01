"""Unit tests for the Tier-0/Tier-1 query-time search improvements in memory_db.

Covers: the get-by-name resolution ladder (exact/slug/case/prefix/ambiguous), the
multi-query union (search_many), the weighted-RRF params + pool floor on
_hybrid_scored, and the bge asymmetric query-instruction prefix.

All tests run against a FRESH temporary DB built once per module (real embeddings;
the fastembed model must be cached; run with HF_HUB_OFFLINE=1). The live memory DB
is never touched. Search is read-only regardless.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_db as mdb  # noqa: E402

# The CLI entry point, exercised as a subprocess for the exit-code contract tests
# (the "did you mean" refusal path uses sys.exit(3), only observable out-of-process).
_MEMDB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_db.py")


# --- Fixture corpus ----------------------------------------------------------
# Rows chosen to exercise the resolver ladder: a human-title-vs-filename-slug row,
# a unique-prefix row, a duplicated name/slug (ambiguous) pair, and case variants.
_ROWS = [
    # path (=> slug via stem),                         name,                              text
    ("instance/meta/feedback_european_style.md", "European academic writing style",
     "No em-dashes in prose; use \\paragraph not \\textbf; European style preference."),
    ("instance/x/alpha-note.md", "alpha-note",
     "Alpha note about widget calibration and sensor drift."),
    ("instance/a/MEMORY.md", "MEMORY", "Memory index A for agent a."),
    ("instance/b/MEMORY.md", "MEMORY", "Memory index B for agent b."),
    ("shared/beta_thing.md", "Beta Thing",
     "Beta thing describes a docker compose orphan recovery procedure."),
    ("shared/gamma.md", "gamma", "Gamma note on matplotlib mathtext bold rendering."),
    ("shared/delta_note.md", "delta-note", "Delta note on cloudflare workers deploy."),
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    p = tmp_path_factory.mktemp("memdb") / ".memory.db"
    mdb.init_db(p)
    for path, name, text in _ROWS:
        mdb.upsert(
            path=path, tier=path.split("/", 1)[0], agent=None, type="feedback",
            name=name, description=text[:60], text=text, db_path=p,
        )
    return p


def _id_for(db, name):
    conn = mdb._connect(db)
    try:
        return conn.execute(
            "SELECT id FROM memories WHERE name = ? ORDER BY id LIMIT 1", (name,)
        ).fetchone()["id"]
    finally:
        conn.close()


def _cli(db, *args):
    """Run the memory_db CLI against the temp DB; return (returncode, stdout, stderr).

    Offline (get/resolve never loads the embedding model) and pointed at the temp DB
    via the global --db override, so the live memory DB is never opened.
    """
    proc = subprocess.run(
        [sys.executable, _MEMDB, "--db", str(db), *args],
        capture_output=True,
        text=True,
        env=dict(os.environ, HF_HUB_OFFLINE="1"),
    )
    return proc.returncode, proc.stdout, proc.stderr


# --- Resolution ladder -------------------------------------------------------


def test_resolve_exact_name(db):
    conn = mdb._connect(db)
    try:
        rid, sug = mdb._resolve_name(conn, "alpha-note")
        assert rid == _id_for(db, "alpha-note")
        assert sug == []
    finally:
        conn.close()


def test_resolve_by_slug_when_name_is_human_title(db):
    # THE canonical bug: stored name is the title, arg is the filename slug.
    conn = mdb._connect(db)
    try:
        rid, sug = mdb._resolve_name(conn, "feedback_european_style")
        assert rid == _id_for(db, "European academic writing style")
        assert sug == []
    finally:
        conn.close()


def test_resolve_case_insensitive_slug(db):
    conn = mdb._connect(db)
    try:
        rid, _ = mdb._resolve_name(conn, "FEEDBACK_EUROPEAN_STYLE")
        assert rid == _id_for(db, "European academic writing style")
    finally:
        conn.close()


def test_resolve_case_insensitive_name(db):
    conn = mdb._connect(db)
    try:
        rid, _ = mdb._resolve_name(conn, "beta thing")  # stored "Beta Thing"
        assert rid == _id_for(db, "Beta Thing")
    finally:
        conn.close()


def test_resolve_unique_prefix(db):
    conn = mdb._connect(db)
    try:
        rid, sug = mdb._resolve_name(conn, "alpha-")  # unique prefix of alpha-note
        assert rid == _id_for(db, "alpha-note")
        assert sug == []
    finally:
        conn.close()


def test_resolve_ambiguous_returns_suggestions_not_a_guess(db):
    # Two rows named MEMORY with slug MEMORY -> must NOT auto-resolve.
    conn = mdb._connect(db)
    try:
        rid, sug = mdb._resolve_name(conn, "MEMORY")
        assert rid is None
        assert len(sug) >= 2
        assert all({"name", "slug", "tier"} <= set(s) for s in sug)
    finally:
        conn.close()


def test_resolve_unknown_returns_none(db):
    conn = mdb._connect(db)
    try:
        rid, _ = mdb._resolve_name(conn, "no-such-note-zzz-qux")
        assert rid is None
    finally:
        conn.close()


def test_slug_of():
    assert mdb._slug_of("instance/meta/feedback_european_style.md") == "feedback_european_style"
    assert mdb._slug_of("/abs/path/to/thing.md") == "thing"
    assert mdb._slug_of("") == ""


# --- Multi-query union -------------------------------------------------------


def test_search_many_single_query_equals_search(db):
    single = mdb.search("gamma matplotlib", k=5, db_path=db)
    many = mdb.search_many(["gamma matplotlib"], k=5, db_path=db)
    assert [r["id"] for r in single] == [r["id"] for r in many]


def test_search_many_str_input_treated_as_one_query(db):
    a = mdb.search("gamma matplotlib", k=5, db_path=db)
    b = mdb.search_many("gamma matplotlib", k=5, db_path=db)
    assert [r["id"] for r in a] == [r["id"] for r in b]


def test_search_many_unions_and_dedups(db):
    euro = _id_for(db, "European academic writing style")
    delta = _id_for(db, "delta-note")
    res = mdb.search_many(
        ["em-dash european prose", "cloudflare workers deploy"], k=8, db_path=db
    )
    ids = [r["id"] for r in res]
    assert euro in ids and delta in ids
    assert len(ids) == len(set(ids))  # deduped


def test_search_many_best_score_and_sorted(db):
    res = mdb.search_many(["docker orphan", "matplotlib bold"], k=8, db_path=db)
    scores = [r["score"] for r in res]
    assert scores == sorted(scores, reverse=True)


def test_search_many_all_blank_returns_empty(db):
    assert mdb.search_many(["", "   "], k=5, db_path=db) == []


# --- Weighted RRF params + pool floor ---------------------------------------


def test_hybrid_scored_params_do_not_crash_and_sort(db):
    conn = mdb._connect(db)
    try:
        out = mdb._hybrid_scored(conn, "european prose", 5,
                                 rrf_k=20, w_fts=1.0, w_vec=1.0, pool=10)
        assert out and all(len(t) == 2 for t in out)
        scores = [s for _, s in out]
        assert scores == sorted(scores, reverse=True)
    finally:
        conn.close()


def test_hybrid_scored_zero_vec_weight_uses_only_fts(db):
    # With w_vec=0, only the FTS arm contributes; a purely-semantic-but-lexically
    # absent doc would score 0. Here we just assert it runs and the top hit is the
    # lexically-matching european note.
    conn = mdb._connect(db)
    try:
        euro = _id_for(db, "European academic writing style")
        out = mdb._hybrid_scored(conn, "em-dash paragraph european", 5,
                                 w_vec=0.0)
        assert out[0][0] == euro
    finally:
        conn.close()


def test_default_constants_in_expected_bands():
    assert 20 <= mdb.RRF_K <= 30
    assert mdb.RRF_W_FTS == mdb.RRF_W_VEC  # equal weights (never down-weight lexical)
    assert mdb.CANDIDATE_POOL >= 30


# --- Asymmetric query prefix -------------------------------------------------


def test_query_prefix_changes_the_query_vector():
    bare = mdb._embed("gpu ran out of memory")
    pref = mdb._embed_query("gpu ran out of memory")
    assert bare != pref  # prefix alters the query embedding


def test_query_prefix_constant_is_bge_instruction():
    assert "searching relevant passages" in mdb.BGE_QUERY_INSTRUCTION


def test_search_vec_path_embeds_query_with_prefix(db, monkeypatch):
    # The PATH assertion (distinct from the constant/vector checks above): the search
    # retrieval path must route the QUERY through _embed_query (asymmetric prefix), not
    # the bare _embed used for stored passages. Spy on _embed_query and confirm the raw
    # query string reaches it during a real search.
    seen = {}
    real = mdb._embed_query

    def spy(text):
        seen["q"] = text
        return real(text)

    monkeypatch.setattr(mdb, "_embed_query", spy)
    mdb.search("gpu ran out of memory", k=3, mode="hybrid", db_path=db)
    assert seen.get("q") == "gpu ran out of memory"


# --- Get-ladder: FTS fallback rung + CLI "did you mean" exit code ------------


def test_resolve_via_fts_fallback_single_candidate(db):
    # Rung 5: 'calibration' is not an exact name/slug, not a case variant, and not a
    # prefix of any name/slug; it appears in exactly ONE description (alpha-note), so
    # the FTS-over-name/description fallback resolves it uniquely (and only then).
    conn = mdb._connect(db)
    try:
        rid, sug = mdb._resolve_name(conn, "calibration")
        assert rid == _id_for(db, "alpha-note")
        assert sug == []
    finally:
        conn.close()


def test_cli_get_resolves_slug_exit_0(db):
    # End-to-end via the CLI: the filename slug of a human-titled note resolves through
    # ladder rung 2 and prints the body with a clean exit.
    rc, out, err = _cli(db, "get", "--name", "feedback_european_style")
    assert rc == 0
    assert "em-dash" in out.lower()


def test_cli_get_ambiguous_name_exits_3_with_did_you_mean(db):
    # Two rows named MEMORY -> the CLI must REFUSE (exit 3) with a 'did you mean' list
    # on stderr and print NOTHING on stdout. It never guesses a body.
    rc, out, err = _cli(db, "get", "--name", "MEMORY")
    assert rc == 3
    assert "did you mean" in err.lower()
    assert out.strip() == ""


def test_cli_get_unknown_name_exits_3(db):
    # An unknown name is also a non-zero refusal, never a silent empty success.
    rc, out, err = _cli(db, "get", "--name", "totally-absent-zzz-qux")
    assert rc == 3
    assert out.strip() == ""


# --- RRF fusion determinism --------------------------------------------------


def test_rrf_score_matches_closed_form(monkeypatch):
    # White-box: feed KNOWN per-arm rankings and assert the fused score equals the
    # closed form  w_fts/(rrf_k+rank_fts) + w_vec/(rrf_k+rank_vec)  exactly, and that
    # the doc present in BOTH arms sorts first. conn is unused once both arms are stubbed.
    monkeypatch.setattr(mdb, "_fts_ranked", lambda c, q, k: [(10, 0.0), (20, 0.0)])
    monkeypatch.setattr(mdb, "_vec_ranked", lambda c, q, k: [(20, 0.0), (30, 0.0)])
    out = dict(mdb._hybrid_scored(None, "q", 8, rrf_k=30, w_fts=1.0, w_vec=1.0, pool=50))
    assert out[10] == pytest.approx(1.0 / 30)             # fts rank 0 only
    assert out[20] == pytest.approx(1.0 / 31 + 1.0 / 30)  # fts rank 1 + vec rank 0
    assert out[30] == pytest.approx(1.0 / 31)             # vec rank 1 only
    ordered = [rid for rid, _ in mdb._hybrid_scored(None, "q", 8, rrf_k=30)]
    assert ordered[0] == 20


def test_rrf_fusion_is_deterministic(db):
    # Same query, same store -> byte-identical (rowid, score) fusion, run to run.
    conn = mdb._connect(db)
    try:
        a = mdb._hybrid_scored(conn, "docker compose orphan recovery", 8)
        b = mdb._hybrid_scored(conn, "docker compose orphan recovery", 8)
        assert a == b
        assert [s for _, s in a] == sorted([s for _, s in a], reverse=True)
    finally:
        conn.close()


def test_search_hybrid_deterministic_end_to_end(db):
    # Determinism across the full search() path (fresh connection each call): identical
    # id + score ordering. Guards against any nondeterminism creeping into fusion/fetch.
    r1 = mdb.search("matplotlib mathtext bold", k=5, mode="hybrid", db_path=db)
    r2 = mdb.search("matplotlib mathtext bold", k=5, mode="hybrid", db_path=db)
    assert [(r["id"], r["score"]) for r in r1] == [(r["id"], r["score"]) for r in r2]


def test_search_many_union_is_order_independent(db):
    # The multi-query UNION is a set operation: the deduped id set must not depend on
    # the order the queries are supplied (complements the dedup test above).
    ab = mdb.search_many(["docker orphan recovery", "matplotlib bold"], k=8, db_path=db)
    ba = mdb.search_many(["matplotlib bold", "docker orphan recovery"], k=8, db_path=db)
    assert {r["id"] for r in ab} == {r["id"] for r in ba}
