"""Test di memory-inject: puntatore statico, recovery slice FTS, percorsi di errore."""
from __future__ import annotations

import sqlite3

import pytest

from sc_hooks import core
from sc_hooks.handlers import memory_inject as mi

# Stesso DDL di scripts/memory/memory_db.py, senza la tabella vec0
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, tier TEXT, agent TEXT, type TEXT, name TEXT,
  description TEXT, text TEXT NOT NULL, html TEXT, created TEXT, updated TEXT, hash TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  name, description, text, content='memories', content_rowid='id'
);
CREATE TABLE IF NOT EXISTS links (src_id INTEGER, dst_name TEXT);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, name, description, text)
  VALUES (new.id, new.name, new.description, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
  VALUES ('delete', old.id, old.name, old.description, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
  VALUES ('delete', old.id, old.name, old.description, old.text);
  INSERT INTO memories_fts(rowid, name, description, text)
  VALUES (new.id, new.name, new.description, new.text);
END;
"""

_FILLER = [(f"filler-{i}", "shared-global", f"unrelated note number {i} about cooking pasta") for i in range(8)]
_ROWS = [
    ("meta-handoff", "instance/meta", "meta recovery context: current state and handoff notes. recovery handoff state."),
    ("weak-hit", "shared-projects", "a long text that mentions recovery only once among many other words " + "lorem " * 60),
    ("archived-handoff", "archive", "meta recovery context current state handoff recovery handoff state"),
    ("no-tier", None, "handoff checklist"),
] + _FILLER


def _make_db(path, rows=_ROWS):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for name, tier, text in rows:
        conn.execute("INSERT INTO memories(path, tier, type, name, description, text) VALUES (?,?,?,?,?,?)",
                     (f"{tier}/{name}.md", tier, "note", name, "", text))
    conn.commit()
    conn.close()


@pytest.fixture
def db(home, monkeypatch):
    path = home / ".claude" / "agent-memory" / ".memory.db"
    monkeypatch.setattr(mi, "DB_PATH", path)
    return path


def _ctx(event="SessionStart", agent="meta"):
    return core.Ctx(event=event, payload={}, session_id="s1", tool="", tool_input={}, cwd="C:/x",
                    role="meta" if agent == "meta" else "plain", agent=agent)


def test_pointer_only_when_db_missing(db):
    res = mi.on_session_start(_ctx())
    assert res.context == [mi.POINTER]
    assert 'mem search "' in mi.POINTER and "mem get <name>" in mi.POINTER
    assert "mem similar <name>" in mi.POINTER and "mem list --tier" in mi.POINTER
    assert "NO MEMORY.md" in mi.POINTER


def test_ranking_and_archive_exclusion(db):
    _make_db(db)
    rows = mi.fts_search("meta recovery context current state handoff")
    names = [r["name"] for r in rows]
    assert names[0] == "meta-handoff"
    assert "archived-handoff" not in names
    assert "no-tier" in names and "weak-hit" in names           # tier NULL non è archivio
    assert len(rows) <= 4 and not any(n.startswith("filler") for n in names)
    assert rows == sorted(rows, key=lambda r: r["score"])      # bm25 crescente = migliore per primo


def test_limit_is_four(db):
    _make_db(db, [(f"h{i}", "shared-global", f"handoff item {i}") for i in range(9)])
    assert len(mi.fts_search("handoff")) == 4


def test_session_start_includes_slice_subagent_does_not(db):
    _make_db(db)
    text = mi.on_session_start(_ctx()).context[0]
    assert text.startswith(mi.POINTER) and "agent=meta" in text and "meta-handoff" in text
    assert "archived-handoff" not in text
    sub = mi.on_subagent_start(_ctx(event="SubagentStart", agent="w-tester")).context[0]
    assert sub == mi.POINTER


@pytest.mark.parametrize("query", ['w-review"er (recovery) AND NOT: handoff*', "NEAR(", '"""', "a-b:c^2 OR"])
def test_punctuation_safe_queries(db, query):
    _make_db(db)
    mi.fts_search(query)                                        # non deve sollevare
    match = mi.fts_match_query(query)
    assert all(tok.startswith('"') and tok.endswith('"') for tok in match.split(" OR ") if tok)


def test_punctuation_query_still_finds_rows(db):
    _make_db(db)
    assert mi.fts_search('(handoff); "recovery"!')[0]["name"] == "meta-handoff"
    assert mi.fts_search("?!...") == []


def test_empty_db_and_corrupt_db_fall_back_to_pointer(db):
    _make_db(db, rows=[])
    assert mi.on_session_start(_ctx()).context == [mi.POINTER]
    db.write_bytes(b"this is not a sqlite file" * 50)
    assert mi.on_session_start(_ctx()).context == [mi.POINTER]


def test_db_without_fts_table(db):
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT)")
    conn.commit()
    conn.close()
    assert mi.on_session_start(_ctx()).context == [mi.POINTER]


def test_output_cap_and_readonly(db):
    _make_db(db, [(f"big{i}", "shared-global", "handoff " + "x" * 50000) for i in range(4)])
    before = db.read_bytes()
    text = mi.on_session_start(_ctx(agent="")).context[0]
    assert len(text) <= mi.MAX_TOTAL and "agent=plain" in text
    assert db.read_bytes() == before


def test_bad_agent_type(db):
    _make_db(db)
    ctx = _ctx()
    ctx.agent = None                                            # type: ignore[assignment]
    assert mi.on_session_start(ctx).context[0].startswith(mi.POINTER)


# Valori di realm memorizzati: oltre a NULL e 'personal', valori scritti fuori dalla
# normalizzazione di memory_db, che una sessione di lavoro non deve vedere
_REALM_ROWS = (("realm-null", None), ("realm-personal", "personal"), ("realm-odd-case", "Personal"),
               ("realm-work", "work"), ("realm-empty", ""))
ALL_REALM_NAMES = sorted(name for name, _ in _REALM_ROWS)


def _make_realm_db(path):
    # DB migrato: colonna realm aggiunta come nello schema futuro (TEXT NULL)
    _make_db(path, rows=[])
    conn = sqlite3.connect(str(path))
    conn.execute("ALTER TABLE memories ADD COLUMN realm TEXT")
    for name, realm in _REALM_ROWS:
        conn.execute(
            "INSERT INTO memories(path, tier, type, name, description, text, realm) VALUES (?,?,?,?,?,?,?)",
            (f"shared-global/{name}.md", "shared-global", "note", name, "", "handoff realmprobe note", realm),
        )
    conn.commit()
    conn.close()


def _names(query="handoff realmprobe"):
    # k oltre SLICE_K: una sessione personale deve vedere tutte le righe di _REALM_ROWS
    return sorted(r["name"] for r in mi.fts_search(query, k=10))


def _set_realm_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SC_MEM_REALM", raising=False)
    else:
        monkeypatch.setenv("SC_MEM_REALM", value)


# Stessa normalizzazione di memory_db._default_realm: strip() e lower()
@pytest.mark.parametrize("value", ["personal", "Personal", " personal", "personal\r", "PERSONAL\n"])
def test_personal_session_injects_every_realm(db, monkeypatch, value):
    _set_realm_env(monkeypatch, value)
    _make_realm_db(db)
    assert _names() == ALL_REALM_NAMES


@pytest.mark.parametrize("value", [None, "", "work", "untagged", "personnel", "personal-x"])
def test_work_session_injects_only_rows_without_realm(db, monkeypatch, value):
    _set_realm_env(monkeypatch, value)
    _make_realm_db(db)
    assert _names() == ["realm-null"]


def test_realm_null_rows_unaffected_in_both_modes(db, monkeypatch):
    _make_realm_db(db)
    for value in (None, "personal"):
        if value is None:
            monkeypatch.delenv("SC_MEM_REALM", raising=False)
        else:
            monkeypatch.setenv("SC_MEM_REALM", value)
        assert "realm-null" in _names()


def test_columnless_db_returns_same_rows_in_both_modes(db, monkeypatch):
    _make_db(db)
    monkeypatch.delenv("SC_MEM_REALM", raising=False)
    default = mi.fts_search("meta recovery context current state handoff")
    monkeypatch.setenv("SC_MEM_REALM", "personal")
    personal = mi.fts_search("meta recovery context current state handoff")
    assert default == personal
    assert [r["name"] for r in default] == ["meta-handoff", "no-tier", "weak-hit"]
