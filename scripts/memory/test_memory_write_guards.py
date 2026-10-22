"""Test delle guardie di scrittura di memory_db: unarchive su un percorso occupato, retier di
una riga archiviata, compact verificato prima del COMMIT (e gemelli a distanza zero),
upsert della CLI senza --path che riusa l'unica riga viva con lo stesso name e tier (e lo
stesso agent se --agent e' dato).

Ogni test lavora su un DB temporaneo esplicito (`--db` o `db_path`): il DB live non viene
mai aperto. Gli embedding sono reali (modello in cache, HF_HUB_OFFLINE=1).
"""
import hashlib
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import memory_db as mdb  # noqa: E402

_MEMDB = os.path.join(_HERE, "memory_db.py")


@pytest.fixture(autouse=True)
def _no_ambient_realm(monkeypatch):
    """Un SC_MEM_REALM ereditato dalla sessione non deve cambiare l'esito dei test."""
    monkeypatch.delenv("SC_MEM_REALM", raising=False)


def _cli(db, *args, env_extra=None):
    env = dict(os.environ, HF_HUB_OFFLINE="1", PYTHONUTF8="1")
    for key in ("MEMORY_DB_PATH", "CLAUDE_DIR", "SC_MEM_REALM"):
        env.pop(key, None)
    env.update(env_extra or {})
    proc = subprocess.run([sys.executable, _MEMDB, "--db", str(db), *args],
                          capture_output=True, text=True, encoding="utf-8", env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _new_db(tmp_path):
    p = tmp_path / "guards.db"
    mdb.init_db(p)
    return p


def _put(db, path, name, tier="instance", agent=None, text=None):
    return mdb.upsert(path=path, tier=tier, agent=agent, type="user", name=name,
                      description="d", text=text or f"body of {path}", db_path=db)


def _state(db):
    """Righe, vettori e link: tutto cio' che un rifiuto non deve toccare."""
    conn = mdb._connect(db, readonly=True)
    try:
        return {
            "rows": [tuple(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")],
            "vec": [(r[0], bytes(r[1])) for r in conn.execute(
                "SELECT rowid, embedding FROM memories_vec ORDER BY rowid")],
            "links": [tuple(r) for r in conn.execute(
                "SELECT src_id, dst_name FROM links ORDER BY src_id, dst_name")],
        }
    finally:
        conn.close()


def _row_state(db, rid):
    """Riga e vettore di una sola memoria, per verificare che resti identica."""
    state = _state(db)
    return ([r for r in state["rows"] if r[0] == rid],
            [v for v in state["vec"] if v[0] == rid])


def _paths(db):
    return [r[0] for r in mdb.run_sql("SELECT path FROM memories ORDER BY id", db)[1]]


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --- unarchive verso un percorso gia' occupato ---------------------------------------


def _taken_origin_db(tmp_path):
    """Una riga archiviata il cui percorso originale e' stato ripreso da una riga viva."""
    p = _new_db(tmp_path)
    old = _put(p, "instance/meta/ctx.md", "ctx", agent="meta", text="old context body")
    mdb.archive(old, db_path=p)
    live = _put(p, "instance/meta/ctx.md", "ctx", agent="meta", text="new context body")
    return p, old, live


def test_cli_unarchive_onto_taken_path_exits_2_and_changes_nothing(tmp_path):
    p, old, live = _taken_origin_db(tmp_path)
    before = _state(p)
    rc, out, err = _cli(p, "archive", "--unarchive", "--id", str(old))
    assert rc == 2, err
    assert out == ""
    assert "Traceback" not in err and "IntegrityError" not in err
    assert len(err.strip().splitlines()) == 1
    assert f"id={live} " in err and "'instance/meta/ctx.md'" in err
    assert "Prune or retier" in err
    assert _state(p) == before


def test_api_unarchive_onto_taken_path_raises_refusal_before_writing(tmp_path):
    p, old, live = _taken_origin_db(tmp_path)
    before = _state(p)
    with pytest.raises(mdb.TierMoveRefusedError, match=f"id={live} "):
        mdb.archive(old, unarchive=True, db_path=p)
    assert issubclass(mdb.TierMoveRefusedError, ValueError)  # i chiamanti esistenti lo catturano
    assert _state(p) == before


def test_unarchive_after_freeing_the_path_restores_the_row(tmp_path):
    p, old, live = _taken_origin_db(tmp_path)
    rc, _, err = _cli(p, "prune", "--id", str(live))
    assert rc == 0, err
    rc, _, err = _cli(p, "archive", "--unarchive", "--id", str(old))
    assert rc == 0, err
    assert _paths(p) == ["instance/meta/ctx.md"]


# --- retier di una riga archiviata ----------------------------------------------------


def _archived_db(tmp_path):
    p = _new_db(tmp_path)
    rid = _put(p, "instance/meta/arch-note.md", "arch-note", agent="meta")
    _put(p, "instance/meta/other.md", "other", agent="meta")
    mdb.archive(rid, db_path=p)
    return p, rid


@pytest.mark.parametrize("selector", ["id", "name"])
def test_cli_retier_of_archived_row_exits_2_and_points_to_unarchive(tmp_path, selector):
    p, rid = _archived_db(tmp_path)
    before = _state(p)
    ref = ("--id", str(rid)) if selector == "id" else ("--name", "arch-note")
    rc, out, err = _cli(p, "retier", *ref, "--tier", "instance")
    assert rc == 2, err
    assert out == ""
    assert f"mem archive --unarchive --id {rid}" in err
    assert _state(p) == before
    assert not any(path.startswith("instance/instance::") for path in _paths(p))


def test_api_retier_of_archived_row_raises_refusal_before_writing(tmp_path):
    p, rid = _archived_db(tmp_path)
    before = _state(p)
    with pytest.raises(mdb.TierMoveRefusedError, match="--unarchive"):
        mdb.retier(rid, "shared-global", db_path=p)
    assert _state(p) == before


# --- archive e retier verso un percorso gia' occupato ---------------------------------


def test_cli_archive_onto_taken_archive_path_exits_2_and_changes_nothing(tmp_path):
    # archivio della riga 1, riga nuova allo stesso percorso, archivio anche di questa:
    # archive/instance::<path> e' gia' della prima
    p = _new_db(tmp_path)
    first = _put(p, "instance/meta/twice.md", "twice", agent="meta", text="first body")
    mdb.archive(first, db_path=p)
    second = _put(p, "instance/meta/twice.md", "twice", agent="meta", text="second body")
    before = _state(p)
    rc, out, err = _cli(p, "archive", "--id", str(second))
    assert rc == 2, err
    assert out == ""
    assert "Traceback" not in err and "IntegrityError" not in err
    assert len(err.strip().splitlines()) == 1
    assert f"id={first} " in err and "'archive/instance::instance/meta/twice.md'" in err
    assert "tier=archive" in err and "nothing changed" in err
    assert f"Prune id={first} first" in err  # una riga archiviata non si sposta con retier
    assert _state(p) == before


def test_cli_retier_onto_taken_path_exits_2_and_changes_nothing(tmp_path):
    p = _new_db(tmp_path)
    src = _put(p, "instance/_/promo.md", "promo-src")
    occ = _put(p, "shared-projects/_/promo.md", "promo-occupant", tier="shared-projects")
    before = _state(p)
    rc, out, err = _cli(p, "retier", "--id", str(src), "--tier", "shared-projects")
    assert rc == 2, err
    assert out == ""
    assert "Traceback" not in err and "IntegrityError" not in err
    assert len(err.strip().splitlines()) == 1
    assert f"id={occ} " in err and "'shared-projects/_/promo.md'" in err
    assert "tier=shared-projects" in err and "nothing changed" in err
    assert f"Prune or retier id={occ} first" in err
    assert _state(p) == before


# --- compact: verifica prima del COMMIT ------------------------------------------------


class _CorruptIntegrity:
    """Connessione vera, ma PRAGMA integrity_check riporta un danno simulato."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def execute(self, sql, *args):
        if sql.strip().lower().startswith("pragma integrity_check"):
            return self._conn.execute("SELECT 'simulated corruption'")
        return self._conn.execute(sql, *args)


def _compact_db(tmp_path):
    p = _new_db(tmp_path)
    ids = [_put(p, f"instance/_/c{i}.md", f"c{i}", text=f"compact probe {i} about topic {i * 7}")
           for i in range(6)]
    # una riga cancellata lascia slot vec0 morti: il compact ha davvero qualcosa da fare
    mdb._prune_row(ids[2], p)
    return p


def test_compact_verification_failure_rolls_back_and_skips_vacuum(tmp_path, monkeypatch):
    p = _compact_db(tmp_path)
    sha_before, before = _sha(p), _state(p)
    real_connect = mdb._connect
    monkeypatch.setattr(mdb, "_connect",
                        lambda db_path=mdb.DB_PATH, readonly=False:
                        _CorruptIntegrity(real_connect(db_path, readonly)))
    with pytest.raises(RuntimeError, match="compact verification FAILED"):
        mdb.compact_db(p)
    monkeypatch.setattr(mdb, "_connect", real_connect)
    # nessun COMMIT e nessun VACUUM: il file e' identico byte per byte
    assert _sha(p) == sha_before
    assert _state(p) == before
    # il DB resta usabile: un compact vero riesce e la ricerca vettoriale funziona
    assert mdb.compact_db(p)["ok"] is True
    assert _state(p) == before
    hits = mdb.search("compact probe 4", k=3, mode="vec", db_path=p)
    assert hits and hits[0]["name"] == "c4"


def _copy_vector(db, src, dsts):
    conn = mdb._connect(db)
    try:
        emb = conn.execute("SELECT embedding FROM memories_vec WHERE rowid = ?", (src,)).fetchone()[0]
        for dst in dsts:
            conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (dst,))
            conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (dst, emb))
        conn.commit()
    finally:
        conn.close()


def test_compact_succeeds_when_sampled_rows_have_identical_vector_twins(tmp_path):
    p = _new_db(tmp_path)
    ids = [_put(p, f"instance/_/t{i}.md", f"t{i}", text=f"twin probe {i} on subject {i * 11}")
           for i in range(6)]
    # campione = primo, centrale, ultimo rowid (ids[0], ids[3], ids[5]): quattro vettori
    # identici (piu' di k=3) comprendono il primo e l'ultimo; il centrale e' unico e ha
    # quei quattro gemelli come vicini a pari distanza, oltre il limite k
    _copy_vector(p, ids[0], [ids[1], ids[2], ids[5]])
    before = _state(p)
    result = mdb.compact_db(p)
    assert result["ok"] is True
    assert _state(p) == before


def test_cli_compact_with_twins_exits_0(tmp_path):
    p = _new_db(tmp_path)
    ids = [_put(p, f"instance/_/u{i}.md", f"u{i}", text=f"cli twin {i} topic {i * 13}")
           for i in range(4)]
    _copy_vector(p, ids[0], [ids[3]])
    rc, out, err = _cli(p, "compact")
    assert rc == 0, err
    assert "compact ok=True" in out


class _RowsCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _WriteAfterVectorSnapshot(_CorruptIntegrity):
    """Connessione vera; subito dopo la copia dei vettori fatta dal compact esegue
    `on_snapshot`, cioe' una scrittura concorrente da un'altra connessione."""

    def __init__(self, conn, on_snapshot):
        super().__init__(conn)
        object.__setattr__(self, "_on_snapshot", on_snapshot)

    def execute(self, sql, *args):
        if "SELECT rowid, embedding FROM memories_vec ORDER BY rowid" in sql:
            rows = self._conn.execute(sql, *args).fetchall()
            self._on_snapshot()
            return _RowsCursor(rows)
        return self._conn.execute(sql, *args)


def test_compact_holds_the_write_lock_from_its_first_read(tmp_path, monkeypatch):
    p = _new_db(tmp_path)
    ids = [_put(p, f"instance/_/w{i}.md", f"w{i}", text=f"lock probe {i} about item {i * 5}")
           for i in range(5)]
    target, donor = ids[1], ids[4]  # ids[1] non e' nel campione KNN (primo, centrale, ultimo)
    before = _state(p)
    outcome = []

    def concurrent_reembed():
        # un upsert che ricalcola il vettore di `target` mentre il compact ha gia' la copia
        other = mdb.sqlite3.connect(str(p), timeout=0)
        try:
            other.enable_load_extension(True)
            mdb.sqlite_vec.load(other)
            emb = other.execute("SELECT embedding FROM memories_vec WHERE rowid = ?",
                                (donor,)).fetchone()[0]
            other.execute("DELETE FROM memories_vec WHERE rowid = ?", (target,))
            other.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                          (target, emb))
            other.commit()
            outcome.append("written")
        except mdb.sqlite3.OperationalError as e:
            outcome.append(str(e))
        finally:
            other.close()

    real_connect = mdb._connect
    monkeypatch.setattr(mdb, "_connect",
                        lambda db_path=mdb.DB_PATH, readonly=False:
                        _WriteAfterVectorSnapshot(real_connect(db_path, readonly),
                                                  concurrent_reembed))
    result = mdb.compact_db(p)
    monkeypatch.setattr(mdb, "_connect", real_connect)
    # la scrittura concorrente aspetta il COMMIT del compact invece di essere sovrascritta
    assert outcome == ["database is locked"]
    assert result["ok"] is True
    assert _state(p) == before


@pytest.mark.parametrize("pre,post,expected", [
    ([(5, 0.0), (9, 0.0), (1, 0.4)], [(9, 0.0), (5, 0.0), (1, 0.4)], True),   # pareggio a zero
    ([(5, 0.0), (1, 0.4), (2, 0.4)], [(5, 0.0), (1, 0.4), (3, 0.4)], True),   # gruppo finale tagliato da k
    ([(5, 0.0), (1, 0.3), (2, 0.4)], [(5, 0.0), (2, 0.3), (1, 0.4)], False),  # rowid scambiati
    ([(5, 0.0), (1, 0.3), (2, 0.4)], [(5, 0.0), (1, 0.3), (2, 0.5)], False),  # distanza cambiata
    ([(9, 0.0), (1, 0.3), (2, 0.4)], [(9, 0.0), (1, 0.3), (2, 0.4)], False),  # 5 non ritrova se stesso
    ([(5, 0.0)], [], False),
])
def test_knn_unchanged(pre, post, expected):
    assert mdb._knn_unchanged(5, pre, post) is expected


# --- upsert della CLI senza --path ---------------------------------------------------


def _body(tmp_path, text):
    f = tmp_path / "body.md"
    f.write_text(text, encoding="utf-8")
    return str(f)


def _cli_upsert(db, tmp_path, name, text, tier="instance", agent=None, path=None,
                env_extra=None, realm=None):
    args = ["upsert", "--tier", tier, "--type", "user", "--name", name,
            "--description", "re-saved", "--text-file", _body(tmp_path, text)]
    if agent is not None:
        args += ["--agent", agent]
    if path is not None:
        args += ["--path", path]
    if realm is not None:
        args += ["--realm", realm]
    return _cli(db, *args, env_extra=env_extra)


@pytest.mark.parametrize("agent,old_path", [("meta", "instance/meta/foo_bar.md"),
                                            (None, "instance/_/foo_bar.md")])
def test_cli_upsert_without_path_updates_the_single_live_namesake(tmp_path, agent, old_path):
    p = _new_db(tmp_path)
    rid = _put(p, old_path, "foo-bar", agent=agent)
    rc, out, err = _cli_upsert(p, tmp_path, "foo-bar", "re-saved body", agent=agent)
    assert rc == 0, err
    assert out.strip() == f"upserted id={rid}  path={old_path}"
    notes = [line for line in err.splitlines() if line.startswith("note:")]
    assert len(notes) == 1 and old_path in notes[0] and f"id={rid}" in notes[0]
    assert "with this name, tier and agent:" in notes[0]  # tier instance: regola esatta
    assert "realm=" not in notes[0]  # stesso realm di una riga nuova: niente da segnalare
    assert _paths(p) == [old_path]
    rc, out, err = _cli(p, "get", "--name", "foo-bar")
    assert rc == 0, err
    assert out.strip() == "re-saved body"


def test_cli_upsert_prefers_the_row_at_the_default_path(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "instance/meta/foo_bar.md", "foo-bar", agent="meta")
    rid = _put(p, "instance/meta/foo-bar.md", "foo-bar", agent="meta")
    rc, out, err = _cli_upsert(p, tmp_path, "foo-bar", "default path body", agent="meta")
    assert rc == 0, err
    assert out.strip() == f"upserted id={rid}  path=instance/meta/foo-bar.md"
    assert "note:" not in err
    assert len(_paths(p)) == 2


def test_cli_upsert_two_live_namesakes_inserts_at_default_path(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "instance/meta/dup_one.md", "dupname", agent="meta")
    _put(p, "instance/meta/dup_two.md", "dupname", agent="meta")
    rc, out, err = _cli_upsert(p, tmp_path, "dupname", "third body", agent="meta")
    assert rc == 0, err
    assert "path=instance/meta/dupname.md" in out
    assert "note:" not in err
    assert len(_paths(p)) == 3


def test_cli_upsert_without_agent_reuses_the_single_live_shared_global_namesake_and_keeps_its_agent(
        tmp_path):
    # le skill salvano shared-global senza --agent righe importate con agent 'meta'
    p = _new_db(tmp_path)
    old_path = "shared-global/meta/feedback_x.md"
    rid = _put(p, old_path, "odd-name", tier="shared-global", agent="meta")
    rc, out, err = _cli_upsert(p, tmp_path, "odd-name", "no agent body", tier="shared-global")
    assert rc == 0, err
    assert out.strip() == f"upserted id={rid}  path={old_path}"
    notes = [line for line in err.splitlines() if line.startswith("note:")]
    assert len(notes) == 1 and f"id={rid}" in notes[0] and "agent=meta" in notes[0]
    assert "with this name and tier:" in notes[0]
    assert mdb.run_sql("SELECT id, path, agent FROM memories", p)[1] == [(rid, old_path, "meta")]
    rc, out, err = _cli(p, "get", "--name", "odd-name")
    assert rc == 0, err
    assert out.strip() == "no agent body"


def test_cli_upsert_without_agent_outside_shared_global_inserts_and_leaves_the_agent_row(tmp_path):
    # fuori da shared-global senza --agent vale solo la riga con agent NULL
    p = _new_db(tmp_path)
    rid = _put(p, "instance/meta/odd_file.md", "odd-name", agent="meta")
    before = _row_state(p, rid)
    rc, out, err = _cli_upsert(p, tmp_path, "odd-name", "no agent body")
    assert rc == 0, err
    assert "path=instance/_/odd-name.md" in out
    assert "note:" not in err
    assert _paths(p) == ["instance/meta/odd_file.md", "instance/_/odd-name.md"]
    assert _row_state(p, rid) == before


def test_cli_upsert_with_an_archived_twin_updates_the_live_namesake_only(tmp_path):
    p = _new_db(tmp_path)
    arch = _put(p, "instance/meta/x_old.md", "x-one", agent="meta", text="archived body")
    mdb.archive(arch, db_path=p)
    live = _put(p, "instance/meta/x_live.md", "x-one", agent="meta", text="live body")
    archived_before = _row_state(p, arch)
    rc, out, err = _cli_upsert(p, tmp_path, "x-one", "updated live body", agent="meta")
    assert rc == 0, err
    assert out.strip() == f"upserted id={live}  path=instance/meta/x_live.md"
    assert mdb.run_sql(f"SELECT text FROM memories WHERE id = {live}", p)[1] == [
        ("updated live body",)]
    assert _row_state(p, arch) == archived_before


@pytest.mark.parametrize("row_realm,session_realm,note_realm", [
    ("personal", None, "personal"),  # riga personal, sessione senza realm
    (None, "personal", "untagged"),  # riga senza realm, sessione SC_MEM_REALM=personal
])
def test_cli_upsert_reuse_keeps_the_row_realm_and_names_it_in_the_note(
        tmp_path, row_realm, session_realm, note_realm):
    p = _new_db(tmp_path)
    old_path = "instance/meta/realm_file.md"
    rid = mdb.upsert(path=old_path, tier="instance", agent="meta", type="user",
                     name="realm-name", description="d", text="realm body", db_path=p,
                     **({"realm": row_realm} if row_realm else {}))
    env = {"SC_MEM_REALM": session_realm} if session_realm else None
    rc, out, err = _cli_upsert(p, tmp_path, "realm-name", "re-saved realm body", agent="meta",
                               env_extra=env)
    assert rc == 0, err
    assert out.strip() == f"upserted id={rid}  path={old_path}"
    notes = [line for line in err.splitlines() if line.startswith("note:")]
    assert len(notes) == 1 and notes[0].endswith(f" realm={note_realm}")
    assert mdb.run_sql("SELECT id, realm FROM memories", p)[1] == [(rid, row_realm)]


def test_cli_upsert_reuse_with_explicit_realm_has_no_realm_suffix(tmp_path):
    p = _new_db(tmp_path)
    old_path = "instance/meta/realm_explicit.md"
    rid = mdb.upsert(path=old_path, tier="instance", agent="meta", type="user",
                     name="realm-explicit", description="d", text="body", db_path=p,
                     realm="personal")
    rc, out, err = _cli_upsert(p, tmp_path, "realm-explicit", "new body", agent="meta",
                               realm="personal")
    assert rc == 0, err
    assert out.strip() == f"upserted id={rid}  path={old_path}"
    notes = [line for line in err.splitlines() if line.startswith("note:")]
    assert len(notes) == 1 and "realm=" not in notes[0]
    assert mdb.run_sql("SELECT id, realm FROM memories", p)[1] == [(rid, "personal")]


def test_cli_upsert_ignores_a_live_tier_namesake_at_an_archive_path(tmp_path):
    p = _new_db(tmp_path)
    # riga di tier instance ma con percorso archive/: esclusa solo da _is_archived_row
    odd = _put(p, "archive/instance::instance/meta/x_old.md", "x-arch", agent="meta",
               text="old body")
    live = _put(p, "instance/meta/x_live.md", "x-arch", agent="meta", text="live body")
    odd_before = _row_state(p, odd)
    rc, out, err = _cli_upsert(p, tmp_path, "x-arch", "updated body", agent="meta")
    assert rc == 0, err
    assert out.strip() == f"upserted id={live}  path=instance/meta/x_live.md"
    assert _row_state(p, odd) == odd_before


def test_cli_upsert_without_agent_and_two_live_namesakes_of_different_agents_inserts(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "shared-global/meta/twin_a.md", "twin-name", tier="shared-global", agent="meta")
    _put(p, "shared-global/other/twin_b.md", "twin-name", tier="shared-global", agent="other")
    before = _paths(p)
    rc, out, err = _cli_upsert(p, tmp_path, "twin-name", "third body", tier="shared-global")
    assert rc == 0, err
    assert "path=shared-global/_/twin-name.md" in out
    assert "note:" not in err
    assert _paths(p) == before + ["shared-global/_/twin-name.md"]


@pytest.mark.parametrize("existing,cli_agent", [
    ({"tier": "shared-projects", "agent": "meta"}, "meta"),  # altro tier
    ({"tier": "instance", "agent": None}, "meta"),           # agent diverso (NULL)
    ({"tier": "instance", "agent": "other"}, "meta"),        # agent diverso
    ({"tier": "shared-projects", "agent": "meta"}, None),    # CLI senza agent, altro tier
    ({"tier": "instance", "agent": "meta", "archived": True}, "meta"),  # solo una archiviata
])
def test_cli_upsert_namesake_with_other_tier_agent_or_archived_inserts_at_default_path(
        tmp_path, existing, cli_agent):
    p = _new_db(tmp_path)
    tier, agent = existing["tier"], existing["agent"]
    rid = _put(p, f"{tier}/{agent or '_'}/odd_file.md", "odd-name", tier=tier, agent=agent)
    if existing.get("archived"):
        mdb.archive(rid, db_path=p)
    before = _paths(p)
    rc, out, err = _cli_upsert(p, tmp_path, "odd-name", "new body", agent=cli_agent)
    assert rc == 0, err
    default = f"instance/{cli_agent or '_'}/odd-name.md"
    assert f"path={default}" in out
    assert "note:" not in err
    assert _paths(p) == before + [default]


def test_cli_upsert_with_explicit_path_never_reuses(tmp_path):
    p = _new_db(tmp_path)
    _put(p, "instance/meta/foo_bar.md", "foo-bar", agent="meta")
    rc, out, err = _cli_upsert(p, tmp_path, "foo-bar", "explicit body", agent="meta",
                               path="instance/meta/foo-bar.md")
    assert rc == 0, err
    assert "path=instance/meta/foo-bar.md" in out
    assert "note:" not in err
    assert _paths(p) == ["instance/meta/foo_bar.md", "instance/meta/foo-bar.md"]


def test_library_upsert_and_upsert_many_keep_keying_on_the_explicit_path(tmp_path):
    # ogni scrittura trova gia' una sola omonima viva con stesso tier e agent: la libreria
    # non la riusa mai, inserisce al percorso dato
    p = _new_db(tmp_path)
    _put(p, "instance/meta/foo_bar.md", "foo-bar", agent="meta")
    row = {"path": "instance/meta/foo-bar.md", "tier": "instance", "agent": "meta",
           "type": "user", "name": "foo-bar", "description": "d", "text": "batch body"}
    conn = mdb._connect(p)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        mdb.upsert_many([row], conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert _paths(p) == ["instance/meta/foo_bar.md", "instance/meta/foo-bar.md"]

    q = _new_db(tmp_path / "api")
    _put(q, "instance/meta/foo_bar.md", "foo-bar", agent="meta")
    _put(q, "instance/meta/foo-bar.md", "foo-bar", agent="meta")
    assert _paths(q) == ["instance/meta/foo_bar.md", "instance/meta/foo-bar.md"]
