"""Test di session-cleanup: stato, log storico, rotazione history.jsonl, backup del DB."""
from __future__ import annotations

import gzip
import os
import sqlite3
import time

import pytest

from sc_hooks import core
from sc_hooks.handlers import session_cleanup as sc


@pytest.fixture
def env(home, monkeypatch):
    c = home / ".claude"
    monkeypatch.setattr(sc, "HISTORY_LOG", c / "logs" / "session-history.log")
    monkeypatch.setattr(sc, "HISTORY_JSONL", c / "history.jsonl")
    monkeypatch.setattr(sc, "HISTORY_BACKUP_DIR", c / "_backups" / "history")
    monkeypatch.setattr(sc, "MEMORY_DB", c / "agent-memory" / ".memory.db")
    monkeypatch.setattr(sc, "MEMORY_BACKUP_DIR", c / "_backups" / "memory")
    return c


def _ctx(sid="sess-1234567890", reason="logout", agent="meta"):
    return core.Ctx(event="SessionEnd", payload={"reason": reason}, session_id=sid, tool="", tool_input={},
                    cwd="C:/x", role="meta" if agent else "plain", agent=agent)


def test_removes_state_dir_and_baseline_files(env):
    ctx = _ctx()
    assert ctx.once("a") and ctx.state_file("x").parent.is_dir()
    other = _ctx(sid="other-session")
    other.once("a")
    tmp = env / "tmp"
    tmp.mkdir()
    for name in ("sess-1234567890-baseline.txt", "sess-1234567890-baseline.diff", "other-session-baseline.txt", "notes.md"):
        (tmp / name).write_text("x")
    assert sc.handle(ctx) is None
    assert not ctx.state_file("x").parent.exists() and other.state_file("x").parent.exists()
    assert sorted(p.name for p in tmp.iterdir()) == ["notes.md", "other-session-baseline.txt"]


def test_history_line_and_trim(env):
    sc.HISTORY_LOG.parent.mkdir(parents=True)
    sc.HISTORY_LOG.write_text("".join(f"old {i}\n" for i in range(600)), encoding="utf-8")
    ctx = _ctx(reason="clear\ninjected")
    ctx.once("a")
    sc.handle(ctx)
    lines = sc.HISTORY_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 500 and lines[0] == "old 101"
    assert "ended: agent=meta duration=0min session=sess-123 exit=clear injected" in lines[-1]
    sc.handle(_ctx(sid="plain-sess", agent=""))
    assert "agent=bare duration=? session=plain-se" in sc.HISTORY_LOG.read_text(encoding="utf-8").splitlines()[-1]


@pytest.mark.parametrize("sid", ["..", ".", "", "../..", "a/../.."])
def test_hostile_session_id_never_escapes_state_dir(env, sid):
    core.STATE_DIR.mkdir(parents=True)
    (core.STATE_DIR / "keep" ).mkdir()
    (env / "settings.json").write_text("{}")
    sc.handle(_ctx(sid=sid))
    assert (env / "settings.json").exists() and core.STATE_DIR.is_dir() and (core.STATE_DIR / "keep").is_dir()


def test_purges_state_dirs_older_than_7_days(env):
    now = time.time()
    for name, age_days in (("fresh", 1), ("old", 8), ("old-but-active", 9)):
        d = core.STATE_DIR / name
        d.mkdir(parents=True)
        (d / "once-x").write_text("1")
        stamp = now - age_days * 86400
        os.utime(d / "once-x", (stamp, stamp))
        os.utime(d, (stamp, stamp))
    (core.STATE_DIR / "old-but-active" / "once-new").write_text("1")
    (core.STATE_DIR / "loose-file").write_text("1")
    sc.handle(_ctx())
    assert sorted(p.name for p in core.STATE_DIR.iterdir()) == ["fresh", "loose-file", "old-but-active"]


def test_history_jsonl_rotation(env, monkeypatch):
    monkeypatch.setattr(sc, "HISTORY_ROTATE_BYTES", 100)
    sc.HISTORY_JSONL.write_text('{"a": 1}\n', encoding="utf-8")
    sc.rotate_history_jsonl()
    assert not sc.HISTORY_BACKUP_DIR.exists()                    # sotto soglia: niente
    payload = b'{"line": "data"}\n' * 50
    sc.HISTORY_JSONL.write_bytes(payload)
    sc.HISTORY_BACKUP_DIR.mkdir(parents=True)
    for i in range(6):
        (sc.HISTORY_BACKUP_DIR / f"history-2020010{i}-000000.jsonl.gz").write_bytes(b"")
    sc.rotate_history_jsonl()
    names = sorted(p.name for p in sc.HISTORY_BACKUP_DIR.iterdir())
    assert len(names) == 5 and names[0] == "history-20200102-000000.jsonl.gz"
    assert gzip.decompress((sc.HISTORY_BACKUP_DIR / names[-1]).read_bytes()) == payload
    assert sc.HISTORY_JSONL.exists() and sc.HISTORY_JSONL.stat().st_size == 0


def _make_db(path, n=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT)")
    conn.executemany("INSERT INTO memories(text) VALUES (?)", [(f"m{i}",) for i in range(n)])
    conn.commit()
    return conn


def test_memory_backup_once_per_day_with_open_writer(env):
    writer = _make_db(sc.MEMORY_DB)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO memories(text) VALUES ('uncommitted')")     # scrittore concorrente aperto
        sc.backup_memory_db()
    finally:
        writer.rollback()
        writer.close()
    dest = sc.MEMORY_BACKUP_DIR / f"memory-{time.strftime('%Y%m%d')}.db"
    conn = sqlite3.connect(str(dest))
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
    conn.close()
    assert not list(sc.MEMORY_BACKUP_DIR.glob("*.part"))
    stamp = dest.stat().st_mtime_ns
    sc.backup_memory_db()                                        # esiste già il backup di oggi
    assert dest.stat().st_mtime_ns == stamp


def test_memory_backup_keeps_newest_10_and_skips_missing_db(env):
    sc.backup_memory_db()
    assert not sc.MEMORY_BACKUP_DIR.exists()                     # DB assente: silenzio
    _make_db(sc.MEMORY_DB).close()
    sc.MEMORY_BACKUP_DIR.mkdir(parents=True)
    for i in range(12):
        (sc.MEMORY_BACKUP_DIR / f"memory-200001{i + 10}.db").write_bytes(b"old")
    (sc.MEMORY_BACKUP_DIR / "memory-notes.txt").write_text("keep")
    (sc.MEMORY_BACKUP_DIR / f"memory-{time.strftime('%Y%m%d')}.db.part").write_bytes(b"stale partial")
    sc.backup_memory_db()
    dbs = sorted(p.name for p in sc.MEMORY_BACKUP_DIR.glob("memory-*.db"))
    assert len(dbs) == 10 and dbs[-1] == f"memory-{time.strftime('%Y%m%d')}.db" and dbs[0] == "20000113".join(["memory-", ".db"])
    assert (sc.MEMORY_BACKUP_DIR / "memory-notes.txt").exists() and not list(sc.MEMORY_BACKUP_DIR.glob("*.part"))


def test_corrupt_db_leaves_no_backup(env):
    sc.MEMORY_DB.parent.mkdir(parents=True)
    sc.MEMORY_DB.write_bytes(b"definitely not sqlite" * 100)
    assert sc.handle(_ctx()) is None
    assert not list(sc.MEMORY_BACKUP_DIR.glob("memory-*"))
