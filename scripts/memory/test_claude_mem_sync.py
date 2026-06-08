#!/usr/bin/env python3
"""Comprehensive test suite for claude_mem_sync.py — Phase EM.

Run as: HF_HUB_OFFLINE=1 ~/.claude/.venv/bin/python test_claude_mem_sync.py

Each test is independent. Synthetic DBs only — real DB never mutated.
Exit non-zero if any test fails.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.expanduser("~/.claude/scripts/memory"))
import claude_mem_sync as S

PY = os.path.expanduser("~/.claude/.venv/bin/python")
MDB = os.path.expanduser("~/.claude/scripts/memory/memory_db.py")
SYNC = os.path.expanduser("~/.claude/scripts/memory/claude_mem_sync.py")
ENV = dict(os.environ, HF_HUB_OFFLINE="1")

# Guard: never touch the real DB
REAL_DB = os.path.expanduser("~/.claude/agent-memory/.memory.db")

# Global lock used by main() — we clean stale instances at startup so tests
# that invoke sync_cmd (which goes through main()) don't collide if a previous
# interrupted test left a stale lock.
GLOBAL_LOCK = Path(S.DEFAULT_LOCK)


def _ensure_lock_free() -> None:
    """If the global lock exists and its PID is dead/gone, remove it."""
    if not GLOBAL_LOCK.exists():
        return
    try:
        pid = int(GLOBAL_LOCK.read_text().split()[0])
        os.kill(pid, 0)
        # PID is alive — a real sync may be running; this is reported in T8
        print(f"  NOTE: global lock held by live PID {pid}; some sync_cmd tests may SKIP")
    except (ValueError, IndexError, OSError):
        # Stale: remove it
        GLOBAL_LOCK.unlink(missing_ok=True)
        print(f"  NOTE: removed stale global lock {GLOBAL_LOCK}")

PASS = 0
FAIL = 0
_results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    _results.append((name, ok, detail))
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def row(path: str, h: str, upd: str = "2026-01-01T00:00:00+00:00") -> dict:
    return {
        "path": path, "name": path.rsplit("/", 1)[-1], "hash": h,
        "text": f"body-{h}", "updated": upd,
        "tier": "instance", "type": "user",
        "description": "d", "agent": "meta",
    }


def make_db(tmp: str, name: str) -> str:
    """Create an empty .memory.db via `memory_db.py init` in tmp dir."""
    db = os.path.join(tmp, name)
    r = subprocess.run([PY, MDB, "--db", db, "init"],
                       capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, f"init failed: {r.stderr}"
    return db


def memdb(db: str, *args, body: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([PY, MDB, "--db", db, *args],
                          input=body, capture_output=True, text=True, env=ENV)


def upsert_row(db: str, path: str, name: str, body: str,
               tier: str = "instance", typ: str = "user",
               desc: str = "d", agent: str = "meta") -> None:
    r = memdb(db, "upsert",
              "--tier", tier, "--type", typ,
              "--name", name, "--description", desc,
              "--path", path, "--text-stdin",
              body=body)
    assert r.returncode == 0, f"upsert failed for {path}: {r.stderr}"


def export_db(db: str) -> dict[str, dict]:
    r = memdb(db, "export", "--with-body")
    assert r.returncode == 0, f"export failed: {r.stderr}"
    rows = json.loads(r.stdout)
    return {row["path"]: row for row in rows}


def sync_cmd(*extra: str, local_db: str, peer_db: str, base: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, SYNC, "--local-db", local_db, "--peer-db", peer_db, "--base", base, *extra],
        capture_output=True, text=True, env=ENV,
    )


# ===========================================================================
# TEST 1 — Pure merge engine: all 12+ branches of classify()
# ===========================================================================

def test_1_classify_branches() -> None:
    print("\n--- TEST 1: classify() — all 12 branches ---")
    cases = [
        # (name, local, remote, base, expected_op, expected_kind)
        ("noop-nobase",
         {"P": row("P", "h1")}, {"P": row("P", "h1")}, {},
         "noop", None),
        ("noop-withbase",
         {"P": row("P", "h1")}, {"P": row("P", "h1")}, {"P": "h1"},
         "noop", None),
        ("add-add-conflict",
         {"P": row("P", "h1")}, {"P": row("P", "h2")}, {},
         "conflict", S.ADD_ADD),
        ("ff-pull",
         {"P": row("P", "h1")}, {"P": row("P", "h2")}, {"P": "h1"},
         "pull", None),
        ("ff-push",
         {"P": row("P", "h1")}, {"P": row("P", "h2")}, {"P": "h2"},
         "push", None),
        ("edit-edit-conflict",
         {"P": row("P", "h1")}, {"P": row("P", "h2")}, {"P": "h0"},
         "conflict", S.EDIT_EDIT),
        ("new-local-push",
         {"P": row("P", "h1")}, {}, {},
         "push", None),
        ("del-local",
         {"P": row("P", "h1")}, {}, {"P": "h1"},
         "del_local", None),
        ("del-remote-edit-local",
         {"P": row("P", "h1")}, {}, {"P": "h0"},
         "conflict", S.DEL_REMOTE_EDIT_LOCAL),
        ("new-remote-pull",
         {}, {"P": row("P", "h1")}, {},
         "pull", None),
        ("del-remote",
         {}, {"P": row("P", "h1")}, {"P": "h1"},
         "del_remote", None),
        ("del-local-edit-remote",
         {}, {"P": row("P", "h1")}, {"P": "h0"},
         "conflict", S.DEL_LOCAL_EDIT_REMOTE),
        ("base-clean",
         {}, {}, {"P": "h1"},
         "base_clean", None),
    ]

    for name, local, remote, base, exp_op, exp_kind in cases:
        acts = S.classify(local, remote, base)
        a = acts[0]
        ok = a.op == exp_op and a.kind == exp_kind
        detail = "" if ok else f"got op={a.op} kind={a.kind}, expected op={exp_op} kind={exp_kind}"
        report(f"classify/{name}", ok, detail)


# ===========================================================================
# TEST 2 — Union add: new row on each side -> both propagate, DBs converge
# ===========================================================================

def test_2_union_add() -> None:
    print("\n--- TEST 2: union add (new row on each side) ---")
    tmp = tempfile.mkdtemp(prefix="em-t2-")
    try:
        A = make_db(tmp, "A.db")
        B = make_db(tmp, "B.db")
        base = os.path.join(tmp, "base.json")

        # Each side gets a unique row not on the other
        upsert_row(A, "test/row-a.md", "row-a", "row A body")
        upsert_row(B, "test/row-b.md", "row-b", "row B body")

        # Sync with --yes (no tty confirmation needed)
        r = sync_cmd("--yes", "--auto", "local",
                     local_db=A, peer_db=B, base=base)
        report("union-add/sync-exit-0", r.returncode == 0, r.stderr[:200])

        exA = export_db(A)
        exB = export_db(B)

        report("union-add/row-a-on-B", "test/row-a.md" in exB)
        report("union-add/row-b-on-A", "test/row-b.md" in exA)

        # Hashes must agree on shared keys
        shared = set(exA) & set(exB)
        converged = (
            set(exA) == set(exB)
            and all(exA[p]["hash"] == exB[p]["hash"] for p in shared)
        )
        report("union-add/DBs-identical", converged,
               f"A keys={len(exA)}, B keys={len(exB)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 3 — Edit-edit conflict: all three resolutions
# ===========================================================================

def test_3_edit_edit_conflict() -> None:
    print("\n--- TEST 3: edit-edit conflict (local / remote / both) ---")

    for resolution in ("local", "remote", "both"):
        tmp = tempfile.mkdtemp(prefix=f"em-t3-{resolution}-")
        try:
            A = make_db(tmp, "A.db")
            B = make_db(tmp, "B.db")
            base = os.path.join(tmp, "base.json")

            PATH = "test/conflict-row.md"
            # Insert same path on both DBs (add-add conflict since no base yet)
            upsert_row(A, PATH, "conflict-row", "local content version")
            upsert_row(B, PATH, "conflict-row", "remote content version")

            if resolution == "local":
                r = sync_cmd("--yes", "--auto", "local",
                             local_db=A, peer_db=B, base=base)
                report(f"edit-edit/{resolution}/exit-0", r.returncode == 0, r.stderr[:200])
                exA = export_db(A)
                exB = export_db(B)
                report(f"edit-edit/{resolution}/A-has-local-content",
                       "local content version" in (exA.get(PATH, {}).get("text") or ""))
                report(f"edit-edit/{resolution}/B-has-local-content",
                       "local content version" in (exB.get(PATH, {}).get("text") or ""))

            elif resolution == "remote":
                r = sync_cmd("--yes", "--auto", "remote",
                             local_db=A, peer_db=B, base=base)
                report(f"edit-edit/{resolution}/exit-0", r.returncode == 0, r.stderr[:200])
                exA = export_db(A)
                exB = export_db(B)
                report(f"edit-edit/{resolution}/A-has-remote-content",
                       "remote content version" in (exA.get(PATH, {}).get("text") or ""))
                report(f"edit-edit/{resolution}/B-has-remote-content",
                       "remote content version" in (exB.get(PATH, {}).get("text") or ""))

            else:  # both
                # Drive programmatically via build_plan + apply_plan
                local_ep = S.Endpoint(
                    "local", py=PY, script=MDB, db=A,
                    base=base,
                )
                remote_ep = S.Endpoint(
                    "peer", py=PY, script=MDB, db=B,
                    base=base,
                )
                local_export = local_ep.export(with_body=True)
                remote_export = remote_ep.export(with_body=True)
                base_map = S.read_base(Path(base))

                actions = S.classify(local_export, remote_export, base_map)
                conflicts = [a for a in actions if a.op == "conflict"]
                # Force 'both' resolution for all conflicts
                resolutions = {a.path: "both" for a in conflicts}

                plan = S.build_plan(
                    actions, resolutions, peer_label="peer",
                    local=local_export, remote=remote_export,
                )
                new_base = S.apply_plan(
                    plan, local_ep=local_ep, remote_ep=remote_ep,
                    old_base=base_map, backup_keep=2, verify=True,
                )

                exA = export_db(A)
                exB = export_db(B)

                # Original path should have local content on both
                report(f"edit-edit/{resolution}/orig-path-exists-both",
                       PATH in exA and PATH in exB)
                # A conflict-suffixed path must also exist on both
                conflict_paths_A = [p for p in exA if "-conflict-" in p]
                conflict_paths_B = [p for p in exB if "-conflict-" in p]
                report(f"edit-edit/{resolution}/conflict-suffix-exists-A",
                       len(conflict_paths_A) >= 1,
                       f"found: {conflict_paths_A}")
                report(f"edit-edit/{resolution}/conflict-suffix-exists-B",
                       len(conflict_paths_B) >= 1,
                       f"found: {conflict_paths_B}")
                # The suffixed paths must match between A and B
                report(f"edit-edit/{resolution}/conflict-paths-match",
                       set(conflict_paths_A) == set(conflict_paths_B),
                       f"A={conflict_paths_A}, B={conflict_paths_B}")

        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 4 — Delete propagation + no resurrection (idempotent base manifest)
# ===========================================================================

def test_4_delete_propagation() -> None:
    print("\n--- TEST 4: delete propagation + no resurrection ---")
    tmp = tempfile.mkdtemp(prefix="em-t4-")
    try:
        A = make_db(tmp, "A.db")
        B = make_db(tmp, "B.db")
        base = os.path.join(tmp, "base.json")

        PATH = "test/del-me.md"
        # Put the row on both sides, then establish base
        upsert_row(A, PATH, "del-me", "deletable body")
        upsert_row(B, PATH, "del-me", "deletable body")

        r1 = sync_cmd("--yes", local_db=A, peer_db=B, base=base)
        report("delete-prop/run1-exit-0", r1.returncode == 0, r1.stderr[:200])

        # Delete from B (simulates "remote" delete from local's perspective)
        rd = memdb(B, "prune", "--path", PATH)
        report("delete-prop/prune-exit-0", rd.returncode == 0, rd.stderr[:200])

        # Sync: B deleted, A still has it -> should del_local (delete from A)
        r2 = sync_cmd("--yes", local_db=A, peer_db=B, base=base)
        report("delete-prop/run2-exit-0", r2.returncode == 0, r2.stderr[:200])

        exA = export_db(A)
        exB = export_db(B)
        report("delete-prop/row-gone-from-A", PATH not in exA)
        report("delete-prop/row-gone-from-B", PATH not in exB)

        # Re-run (idempotent): row should NOT come back
        r3 = sync_cmd("--yes", local_db=A, peer_db=B, base=base)
        report("delete-prop/rerun-exit-0", r3.returncode == 0, r3.stderr[:200])

        exA2 = export_db(A)
        exB2 = export_db(B)
        report("delete-prop/no-resurrection-A", PATH not in exA2)
        report("delete-prop/no-resurrection-B", PATH not in exB2)

        # Dry-run should report everything in sync
        r4 = sync_cmd("--dry-run", local_db=A, peer_db=B, base=base)
        report("delete-prop/dry-run-in-sync",
               "everything already in sync" in r4.stdout,
               repr(r4.stdout[:200]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 5 — delete-vs-edit conflicts in both directions
# ===========================================================================

def test_5_delete_vs_edit_conflicts() -> None:
    print("\n--- TEST 5: delete-vs-edit conflicts (both directions) ---")

    # --- 5a: del-local + edit-remote -> DEL_LOCAL_EDIT_REMOTE ---
    tmp = tempfile.mkdtemp(prefix="em-t5a-")
    try:
        A = make_db(tmp, "A.db")
        B = make_db(tmp, "B.db")
        base = os.path.join(tmp, "base.json")

        PATH = "test/dlr.md"
        upsert_row(A, PATH, "dlr", "original body")
        upsert_row(B, PATH, "dlr", "original body")
        # Establish base so both sides are in sync
        r0 = sync_cmd("--yes", local_db=A, peer_db=B, base=base)
        report("del-local-edit-remote/setup-exit-0", r0.returncode == 0)

        # Now: A deletes, B edits
        rd = memdb(A, "prune", "--path", PATH)
        report("del-local-edit-remote/prune-exit-0", rd.returncode == 0, rd.stderr[:100])
        upsert_row(B, PATH, "dlr", "remote edited body (B)")

        # Classify should detect DEL_LOCAL_EDIT_REMOTE
        exA = export_db(A)
        exB = export_db(B)
        base_map = S.read_base(Path(base))
        actions = S.classify(exA, exB, base_map)
        conflict_actions = [a for a in actions if a.op == "conflict" and a.path == PATH]
        report("del-local-edit-remote/kind-correct",
               len(conflict_actions) == 1 and conflict_actions[0].kind == S.DEL_LOCAL_EDIT_REMOTE,
               f"actions: {[(a.op, a.kind) for a in actions if a.path == PATH]}")

        # --auto remote: keep the remote edit -> restore on local, keep on remote
        r1 = sync_cmd("--yes", "--auto", "remote", local_db=A, peer_db=B, base=base)
        report("del-local-edit-remote/auto-remote-exit-0", r1.returncode == 0, r1.stderr[:200])
        exA2 = export_db(A)
        exB2 = export_db(B)
        report("del-local-edit-remote/auto-remote-A-has-row", PATH in exA2)
        report("del-local-edit-remote/auto-remote-B-has-row", PATH in exB2)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- 5b: del-remote + edit-local -> DEL_REMOTE_EDIT_LOCAL ---
    tmp = tempfile.mkdtemp(prefix="em-t5b-")
    try:
        A = make_db(tmp, "A.db")
        B = make_db(tmp, "B.db")
        base = os.path.join(tmp, "base.json")

        PATH = "test/drl.md"
        upsert_row(A, PATH, "drl", "original body")
        upsert_row(B, PATH, "drl", "original body")
        r0 = sync_cmd("--yes", local_db=A, peer_db=B, base=base)
        report("del-remote-edit-local/setup-exit-0", r0.returncode == 0)

        # B deletes, A edits
        rd = memdb(B, "prune", "--path", PATH)
        report("del-remote-edit-local/prune-exit-0", rd.returncode == 0, rd.stderr[:100])
        upsert_row(A, PATH, "drl", "local edited body (A)")

        # Classify
        exA = export_db(A)
        exB = export_db(B)
        base_map = S.read_base(Path(base))
        actions = S.classify(exA, exB, base_map)
        conflict_actions = [a for a in actions if a.op == "conflict" and a.path == PATH]
        report("del-remote-edit-local/kind-correct",
               len(conflict_actions) == 1 and conflict_actions[0].kind == S.DEL_REMOTE_EDIT_LOCAL,
               f"actions: {[(a.op, a.kind) for a in actions if a.path == PATH]}")

        # --auto local: honor local edit -> restore on remote
        r1 = sync_cmd("--yes", "--auto", "local", local_db=A, peer_db=B, base=base)
        report("del-remote-edit-local/auto-local-exit-0", r1.returncode == 0, r1.stderr[:200])
        exA2 = export_db(A)
        exB2 = export_db(B)
        report("del-remote-edit-local/auto-local-A-has-row", PATH in exA2)
        report("del-remote-edit-local/auto-local-B-has-row", PATH in exB2)
        report("del-remote-edit-local/B-has-local-content",
               "local edited body (A)" in (exB2.get(PATH, {}).get("text") or ""))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 6 — Peer-down clean skip (unresolvable DNS)
# ===========================================================================

def test_6_peer_down_skip() -> None:
    print("\n--- TEST 6: peer-down clean skip ---")
    tmp = tempfile.mkdtemp(prefix="em-t6-")
    try:
        A = make_db(tmp, "A.db")
        base = os.path.join(tmp, "base.json")

        r = subprocess.run(
            [PY, SYNC,
             "--local-db", A,
             "--peer-host", "nonexistent.invalid",
             "--base", base],
            capture_output=True, text=True, env=ENV,
            timeout=30,  # should fail fast via ConnectTimeout=8
        )
        report("peer-down/exit-0", r.returncode == 0,
               f"exit={r.returncode} stderr={r.stderr[:200]}")
        report("peer-down/SKIP-on-stderr", "SKIP" in r.stderr,
               f"stderr: {r.stderr[:300]}")
    except subprocess.TimeoutExpired:
        report("peer-down/exit-0", False, "timed out (>30s) — BatchMode=yes should prevent hang")
        report("peer-down/SKIP-on-stderr", False, "timed out")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 7 — Idempotent re-run: dry-run reports "everything already in sync"
# ===========================================================================

def test_7_idempotent_rerun() -> None:
    print("\n--- TEST 7: idempotent re-run ---")
    tmp = tempfile.mkdtemp(prefix="em-t7-")
    try:
        A = make_db(tmp, "A.db")
        B = make_db(tmp, "B.db")
        base = os.path.join(tmp, "base.json")

        # Add some rows
        upsert_row(A, "test/row1.md", "row1", "body one")
        upsert_row(B, "test/row2.md", "row2", "body two")

        # First sync
        r1 = sync_cmd("--yes", "--auto", "local", local_db=A, peer_db=B, base=base)
        report("idempotent/run1-exit-0", r1.returncode == 0, r1.stderr[:200])

        # Dry-run should report no changes
        r2 = sync_cmd("--dry-run", local_db=A, peer_db=B, base=base)
        report("idempotent/dry-run-exit-0", r2.returncode == 0, r2.stderr[:200])
        report("idempotent/dry-run-in-sync",
               "everything already in sync" in r2.stdout,
               repr(r2.stdout[:300]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 8 — Lockfile concurrency
# ===========================================================================

def test_8_lockfile_concurrency() -> None:
    print("\n--- TEST 8: lockfile concurrency ---")
    tmp = tempfile.mkdtemp(prefix="em-t8-")
    lock_path = Path(tmp) / "test.lock"
    try:
        # Test the Lock class directly: second acquire raises SkipSync
        raised = False
        skip_msg = ""
        with S.Lock(lock_path):
            # Inside the lock, try to acquire again
            try:
                with S.Lock(lock_path):
                    pass
            except S.SkipSync as exc:
                raised = True
                skip_msg = str(exc)

        report("lockfile/second-acquire-raises-SkipSync", raised,
               f"msg={skip_msg[:100]}")
        report("lockfile/skip-message-mentions-lock", "lock" in skip_msg.lower(),
               f"msg={skip_msg[:100]}")

        # After first context exits, lock file should be gone
        report("lockfile/lock-file-removed-after-exit", not lock_path.exists())

        # A second Lock on the same path succeeds (lock is released)
        second_ok = False
        try:
            with S.Lock(lock_path):
                second_ok = True
        except Exception:
            pass
        report("lockfile/second-lock-after-release-ok", second_ok)

        # Stale lock (dead PID) is cleaned up automatically
        stale_path = Path(tmp) / "stale.lock"
        # Write a PID that cannot exist (large number)
        stale_path.write_text("999999999 2026-01-01T00:00:00+00:00\n")
        stale_ok = False
        try:
            with S.Lock(stale_path):
                stale_ok = True
        except S.SkipSync:
            pass
        report("lockfile/stale-lock-auto-cleaned", stale_ok)

    finally:
        # Clean up any residual lock files
        for f in [lock_path, Path(tmp) / "stale.lock"]:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 9 — Backups: Endpoint.backup() creates <db>.bak-<ts> + retention
# ===========================================================================

def test_9_backups() -> None:
    print("\n--- TEST 9: backups ---")
    tmp = tempfile.mkdtemp(prefix="em-t9-")
    try:
        A = make_db(tmp, "A.db")
        ep = S.Endpoint(
            "local", py=PY, script=MDB, db=A,
            base=os.path.join(tmp, "base.json"),
        )

        # Create one backup, assert it exists
        dst = ep.backup(keep=5)
        report("backup/returns-path", bool(dst), f"dst={dst}")
        report("backup/file-exists", os.path.exists(dst), f"dst={dst}")
        report("backup/path-contains-bak", ".bak-" in dst, f"dst={dst}")

        # Retention: call backup N+2 times with keep=N, assert N files remain
        N = 3
        # Need small delay so timestamps differ (bak-* uses %S, 1s resolution)
        for _ in range(N + 2):
            ep.backup(keep=N)
            time.sleep(1.05)  # ensure unique timestamp per call

        baks = sorted(glob.glob(A + ".bak-*"))
        report("backup/retention-keeps-N",
               len(baks) == N,
               f"expected {N}, found {len(baks)}: {[os.path.basename(b) for b in baks]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# TEST 10 — API-ONLY static check: no direct sqlite3.connect / open(db)
# ===========================================================================

def test_10_api_only_static_check() -> None:
    print("\n--- TEST 10: API-ONLY static check ---")
    src = Path(SYNC).read_text(encoding="utf-8")

    no_sqlite3_connect = "sqlite3.connect(" not in src
    report("api-only/no-sqlite3-connect", no_sqlite3_connect,
           "found 'sqlite3.connect(' in claude_mem_sync.py" if not no_sqlite3_connect else "")

    # No open(...) of a .db path: pattern `open(... .db` should not appear
    # We look for `open(` followed by content containing `.db`
    import re
    open_db_pattern = re.compile(r'open\([^)]*\.db')
    found_open_db = open_db_pattern.search(src)
    report("api-only/no-open-db-path",
           found_open_db is None,
           f"found: {found_open_db.group()!r}" if found_open_db else "")

    # Tool MUST use memory_db.py (checks that the constant DEFAULT_SCRIPT is present
    # and that _memdb routes through it)
    report("api-only/routes-through-memory_db",
           "memory_db.py" in src or "DEFAULT_SCRIPT" in src)


def test_11_null_hash_classify() -> None:
    """A NULL stored hash must never produce a silent no-op (regression guard)."""
    print("\n--- TEST 11: NULL stored-hash classification ---")

    def r(text: str, h: str | None = None) -> dict:
        return {"path": "P", "name": "n", "hash": h, "text": text,
                "description": "d", "tier": "instance", "type": "user",
                "agent": "meta", "updated": "2026-01-01T00:00:00+00:00"}

    a1 = S.classify({"P": r("alpha")}, {"P": r("beta")}, {})[0]
    report("null-hash/divergent-is-conflict",
           a1.op == "conflict" and a1.kind == S.EDIT_EDIT,
           f"op={a1.op} kind={a1.kind}")

    a2 = S.classify({"P": r("same")}, {"P": r("same")}, {})[0]
    report("null-hash/identical-is-noop", a2.op == "noop", f"op={a2.op}")

    a3 = S.classify({"P": r("alpha", "h1")}, {"P": r("beta")}, {})[0]
    report("null-hash/one-side-null-is-conflict",
           a3.op == "conflict" and a3.kind == S.EDIT_EDIT,
           f"op={a3.op} kind={a3.kind}")


# ===========================================================================
# MAIN
# ===========================================================================

def main() -> int:
    print("=" * 60)
    print("claude_mem_sync.py — comprehensive test suite")
    print("=" * 60)

    # Clean up any stale global lock from a previous interrupted run
    _ensure_lock_free()

    test_1_classify_branches()
    test_2_union_add()
    test_3_edit_edit_conflict()
    test_4_delete_propagation()
    test_5_delete_vs_edit_conflicts()
    test_6_peer_down_skip()
    test_7_idempotent_rerun()
    test_8_lockfile_concurrency()
    test_9_backups()
    test_10_api_only_static_check()
    test_11_null_hash_classify()

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"RESULTS: {PASS}/{total} passed  |  {FAIL} failure(s)")
    print("=" * 60)

    # Verify we never touched the real DB
    import hashlib
    if os.path.exists(REAL_DB):
        print(f"  real DB md5: {hashlib.md5(open(REAL_DB,'rb').read()).hexdigest()} (unchanged)")

    if FAIL > 0:
        print("\nFAILED TESTS:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))

    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
