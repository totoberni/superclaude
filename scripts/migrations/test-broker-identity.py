#!/usr/bin/env python3
"""Regression suite for the broker bus-identity layer (DIR-002 T1f).

Runs entirely on sacrificial DB copies in $TMPDIR — never touches the live
broker. Covers:
  (i)   direct-send then backfill of the same identity -> 1 row,
        skipped_existing=1, divergent_body flagged
  (ii)  double direct-send -> 1 row + no-op notice (library + CLI)
  (iii) NUDGE/EVENT NULL-seq x2 -> both insert
  (iv)  backfill re-run -> applied=0 (idempotent)
  (v)   migration rehearsal on synthetic dups -> keeps lowest id,
        read_at coalesced
  (vi)  concurrency smoke: send during an open writer transaction ->
        clean serialization, no corruption

Usage: python3 test-broker-identity.py
Exit 0 = all pass.
"""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

SCRIPTS = os.path.expanduser("~/.claude/scripts")
LIVE_DB = os.path.expanduser("~/.claude/comms/.broker.db")
MIGRATION = os.path.join(SCRIPTS, "migrations", "2026-06-13-broker-identity.py")
BROKER_PY = os.path.join(SCRIPTS, "hcom-broker.py")

sys.path.insert(0, SCRIPTS)
import hcom_backfill  # noqa: E402

_spec = importlib.util.spec_from_file_location("hcom_broker", BROKER_PY)
hcom_broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hcom_broker)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def fresh_copy(tmp, tag):
    db = os.path.join(tmp, f"{tag}.db")
    shutil.copy(LIVE_DB, db)
    # strip WAL sidecars if any were copied alongside
    return db


def count_identity(db, frm, to, kind, seq):
    c = sqlite3.connect(db)
    n = c.execute(
        "SELECT COUNT(*) FROM messages WHERE from_agent=? AND to_agent=? AND kind=? AND seq=?",
        (frm, to, kind, seq),
    ).fetchone()[0]
    c.close()
    return n


def main():
    tmp = tempfile.mkdtemp(prefix="broker-identity-test-", dir=os.environ.get("TMPDIR", "/tmp"))

    # --- (i) direct-send -> backfill same identity ---
    print("(i) direct-send then backfill of same identity")
    db = fresh_copy(tmp, "t1")
    b = hcom_broker.Broker(db)
    mid = b.send("meta", "RPT", "condensed direct-send body", seq=99, from_agent="test-orch")
    flat = os.path.join(tmp, "reports.md")
    with open(flat, "w") as f:
        f.write("## RPT-99\n\n**Time**: 2026-06-13 11:00\nFull flat-file body — much longer than the condensed send.\n")
    res = hcom_backfill.process_file(flat, "test-orch", "RPT", "test-orch", "meta", db, "apply")
    applied, skipped, divergent, would, total = res
    check("one row on bus", count_identity(db, "test-orch", "meta", "RPT", 99) == 1)
    check("skipped_existing=1", skipped == 1, f"got {skipped}")
    check("divergent_body flagged", divergent == 1, f"got {divergent}")
    check("applied=0", applied == 0, f"got {applied}")
    # dry-run surfaces the same divergence
    res_dry = hcom_backfill.process_file(flat, "test-orch", "RPT", "test-orch", "meta", db, "dry-run")
    check("dry-run surfaces divergence too", res_dry[1] == 1 and res_dry[2] == 1,
          f"skipped={res_dry[1]} divergent={res_dry[2]}")

    # --- (ii) double direct-send ---
    print("(ii) double direct-send")
    db = fresh_copy(tmp, "t2")
    b = hcom_broker.Broker(db)
    first = b.send("meta", "RPT", "body A", seq=77, from_agent="test-orch")
    second = b.send("meta", "RPT", "body A re-sent", seq=77, from_agent="test-orch")
    check("first send returns id", first is not None)
    check("second send returns None (no-op)", second is None)
    check("one row on bus", count_identity(db, "test-orch", "meta", "RPT", 77) == 1)
    # CLI path: exit 0 + stderr notice
    cli = subprocess.run(
        [sys.executable, BROKER_PY, "--db", db, "send", "--to", "meta",
         "--kind", "RPT", "--seq", "77", "--body", "x", "--from", "test-orch"],
        capture_output=True, text=True,
    )
    check("CLI re-send exits 0", cli.returncode == 0, f"rc={cli.returncode}")
    check("CLI stderr says already-on-bus", "already-on-bus" in cli.stderr, cli.stderr.strip())

    # --- (iii) NULL-seq kinds unconstrained ---
    print("(iii) NUDGE/EVENT NULL-seq x2")
    db = fresh_copy(tmp, "t3")
    b = hcom_broker.Broker(db)
    ids = [b.send("scaf", k, "ping", seq=None, from_agent="test-meta") for k in ("NUDGE", "NUDGE", "EVENT", "EVENT")]
    check("all four NULL-seq sends insert", all(i is not None for i in ids), str(ids))

    # --- (iv) backfill re-run idempotent ---
    print("(iv) backfill re-run idempotent")
    db = fresh_copy(tmp, "t4")
    flat2 = os.path.join(tmp, "reports2.md")
    with open(flat2, "w") as f:
        f.write("## RPT-101\n\nbody one\n\n## RPT-102\n\nbody two\n")
    r1 = hcom_backfill.process_file(flat2, "test-orch", "RPT", "test-orch", "meta", db, "apply")
    r2 = hcom_backfill.process_file(flat2, "test-orch", "RPT", "test-orch", "meta", db, "apply")
    check("first run applies 2", r1[0] == 2, f"got {r1[0]}")
    check("re-run applies 0", r2[0] == 0, f"got {r2[0]}")
    check("re-run skips 2, no divergence", r2[1] == 2 and r2[2] == 0,
          f"skipped={r2[1]} divergent={r2[2]}")

    # --- (v) migration rehearsal on synthetic dups ---
    print("(v) migration on synthetic dups")
    db = os.path.join(tmp, "t5.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts INTEGER, from_agent TEXT,"
              " to_agent TEXT, kind TEXT, seq INTEGER, body TEXT, read_at INTEGER)")
    c.executemany(
        "INSERT INTO messages (id, ts, from_agent, to_agent, kind, seq, body, read_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 10, "a", "b", "DIR", 1, "keeper, unread", None),
            (2, 11, "a", "b", "DIR", 1, "dup, read", 555),
            (3, 12, "a", "b", "DIR", 1, "dup, read earlier", 444),
            (4, 13, "x", "y", "RPT", 2, "non-dup", None),
        ],
    )
    c.commit(); c.close()
    mig = subprocess.run([sys.executable, MIGRATION, db], capture_output=True, text=True)
    c = sqlite3.connect(db)
    rows = c.execute("SELECT id, read_at FROM messages ORDER BY id").fetchall()
    idx = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='idx_messages_identity'").fetchone()[0]
    c.close()
    check("migration exits 0", mig.returncode == 0, f"rc={mig.returncode}")
    check("keeps lowest id only", [r[0] for r in rows] == [1, 4], str(rows))
    check("read_at coalesced to MAX(555)", rows[0][1] == 555, str(rows[0]))
    check("index created", idx == 1)
    check("removal log has 2 lines", mig.stdout.count("REMOVE id=") == 2)

    # --- (vi) concurrency smoke ---
    print("(vi) send while a backfill transaction is open")
    db = fresh_copy(tmp, "t6")
    # check_same_thread=False: the release thread issues the COMMIT
    holder = sqlite3.connect(db, timeout=30.0, isolation_level=None, check_same_thread=False)
    holder.execute("PRAGMA busy_timeout=30000")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO messages (ts, from_agent, to_agent, kind, seq, body)"
                   " VALUES (1, 'txn-holder', 'meta', 'RPT', 500, 'in-flight backfill')")

    def release():
        time.sleep(2.0)
        holder.execute("COMMIT")

    t = threading.Thread(target=release)
    t.start()
    t0 = time.time()
    b = hcom_broker.Broker(db)
    mid = b.send("meta", "RPT", "sent during open txn", seq=501, from_agent="test-orch")
    waited = time.time() - t0
    t.join()
    holder.close()
    c = sqlite3.connect(db)
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    n = c.execute("SELECT COUNT(*) FROM messages WHERE seq IN (500, 501)"
                  " AND from_agent IN ('txn-holder','test-orch')").fetchone()[0]
    c.close()
    check("send serialized cleanly (waited for lock)", mid is not None, f"waited {waited:.1f}s")
    check("both rows landed", n == 2, f"got {n}")
    check("integrity ok", ok == "ok", ok)

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'ALL_PASS' if not failed else 'FAIL'}: {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
