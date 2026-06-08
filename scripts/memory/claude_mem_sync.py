#!/usr/bin/env python3
"""claude-mem-sync — cross-device 3-way memory reconciliation (superclaude Phase EM).

Reconciles two `~/.claude/agent-memory/.memory.db` stores (peer <-> WSL) so they
never diverge, with per-entry control when the *same* memory differs on both sides.

Design invariants (do not break — they are what make this safe + durable):

  * API-ONLY: every read/write/delete goes through the `memory_db.py` CLI
    (`export` / `upsert` / `prune`). The vec0 + FTS5 virtual tables are NEVER
    touched directly, so the tool survives sqlite-vec / Python / OS upgrades —
    the API layer absorbs on-disk format changes.
  * IDENTITY = `path` (the UNIQUE column). EQUALITY = stored content `hash`
    (sha256 of name+description+text, computed inside memory_db.py — machine
    independent). RECENCY = `updated`. The tool reads stored hashes and never
    recomputes them.
  * 3-WAY MERGE vs a base manifest (last agreed {path: hash}) so a delete on one
    side propagates instead of being resurrected by a naive union.
  * The new base manifest is rebuilt from a POST-APPLY re-export of the local
    side (authoritative stored hashes) rather than from any hash this tool
    computes — no mirrored hash formula to drift (single source of truth).

Topology: the only guaranteed channel is WSL -> peer, so this runs ON WSL with
local = WSL DB and peer = peer over SSH. The Endpoint abstraction makes both
sides uniform, so the whole engine is exercisable on a single host by pointing
`--peer-db` at a second local DB copy (no SSH) — that is also the test harness.

Usage:
  claude-mem-sync --peer-host peer                # the real WSL->peer sync
  claude-mem-sync --peer-db /tmp/other.db --dry-run
  claude-mem-sync --peer-host peer --auto newer   # non-interactive
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# --- Constants ---------------------------------------------------------------

HOME = Path.home()
DEFAULT_PY = HOME / ".claude" / ".venv" / "bin" / "python"
DEFAULT_SCRIPT = HOME / ".claude" / "scripts" / "memory" / "memory_db.py"
DEFAULT_DB = HOME / ".claude" / "agent-memory" / ".memory.db"
DEFAULT_BASE = HOME / ".claude" / "agent-memory" / ".sync-base.json"
DEFAULT_LOCK = HOME / ".claude" / "agent-memory" / ".sync.lock"
BACKUP_KEEP = 10

# Remote-relative locations (resolved against the peer's $HOME, discovered live).
REL_PY = ".claude/.venv/bin/python"
REL_SCRIPT = ".claude/scripts/memory/memory_db.py"
REL_DB = ".claude/agent-memory/.memory.db"
REL_BASE = ".claude/agent-memory/.sync-base.json"

# Default SSH options: key-only (BatchMode) so an unreachable/locked peer fails
# fast instead of hanging on a prompt; connection multiplexing makes the many
# small per-row calls cheap.
DEFAULT_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/cm-memsync-%r@%h:%p",
    "-o", "ControlPersist=30s",
]

# Run memory_db.py fully offline (no embedding model download on upsert).
ENV_PREFIX = ["env", "HF_HUB_OFFLINE=1"]


# --- Errors ------------------------------------------------------------------


class EndpointError(RuntimeError):
    """A memory_db.py invocation on an endpoint failed."""


class SkipSync(RuntimeError):
    """Non-fatal: peer unreachable / lock held — skip cleanly (exit 0)."""


class SyncAbort(RuntimeError):
    """User aborted (q) or a precondition failed — mutate nothing."""


# --- Endpoint abstraction ----------------------------------------------------


class Endpoint:
    """Uniform access to one memory DB via the memory_db.py CLI.

    Local: runs the venv python as a subprocess. Remote: runs the peer's venv
    python over SSH. The peer's absolute paths are resolved from its live $HOME
    (the same probe doubles as the reachability + timeout check).
    """

    def __init__(self, label: str, *, py: str, script: str, db: str,
                 base: str, ssh_host: str | None = None,
                 ssh_opts: list[str] | None = None):
        self.label = label
        self.py = py
        self.script = script
        self.db = db
        self.base = base
        self.ssh_host = ssh_host
        self.ssh_opts = ssh_opts or []

    @property
    def is_remote(self) -> bool:
        return self.ssh_host is not None

    # -- low-level exec -------------------------------------------------------

    def _exec(self, argv: list[str], *, input_text: str | None = None,
              timeout: float | None = None) -> str:
        """Run argv locally, or shlex-quoted over SSH. Raise on non-zero exit."""
        if self.is_remote:
            remote_cmd = " ".join(shlex.quote(a) for a in argv)
            full = ["ssh", *self.ssh_opts, self.ssh_host, remote_cmd]
        else:
            full = argv
        try:
            proc = subprocess.run(
                full, input=input_text, capture_output=True, text=True,
                encoding="utf-8", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise EndpointError(f"[{self.label}] timed out: {argv[:4]}") from exc
        if proc.returncode != 0:
            raise EndpointError(
                f"[{self.label}] {' '.join(argv[2:6])} -> exit {proc.returncode}: "
                f"{proc.stderr.strip()[:400]}"
            )
        return proc.stdout

    def _memdb(self, sub_args: list[str], *, input_text: str | None = None,
               timeout: float | None = None) -> str:
        argv = [*ENV_PREFIX, self.py, self.script, "--db", self.db, *sub_args]
        return self._exec(argv, input_text=input_text, timeout=timeout)

    # -- reachability ---------------------------------------------------------

    def probe(self) -> None:
        """Verify the endpoint answers; raise SkipSync if a remote is down."""
        if not self.is_remote:
            if not Path(self.db).exists():
                raise SyncAbort(f"local DB not found: {self.db}")
            return
        try:
            out = subprocess.run(
                ["ssh", *self.ssh_opts, self.ssh_host, "echo memsync-ok"],
                capture_output=True, text=True, encoding="utf-8", timeout=12,
            )
        except subprocess.TimeoutExpired as exc:
            raise SkipSync(f"peer {self.ssh_host!r} unreachable (timeout)") from exc
        if out.returncode != 0 or "memsync-ok" not in out.stdout:
            raise SkipSync(
                f"peer {self.ssh_host!r} unreachable: {out.stderr.strip()[:200]}"
            )

    # -- operations -----------------------------------------------------------

    def export(self, with_body: bool = True) -> dict[str, dict]:
        """Return {path: row} for every memory (row includes `text` if with_body)."""
        args = ["export"] + (["--with-body"] if with_body else [])
        rows = json.loads(self._memdb(args))
        return {r["path"]: r for r in rows}

    def upsert(self, row: dict) -> None:
        """Insert/update a row by its `path` (recomputes embedding + hash)."""
        args = [
            "upsert",
            "--tier", row.get("tier") or "",
            "--type", row.get("type") or "",
            "--name", row.get("name") or "",
            "--description", row.get("description") or "",
            "--path", row["path"],
            "--text-stdin",
        ]
        if row.get("agent"):
            args += ["--agent", row["agent"]]
        self._memdb(args, input_text=row.get("text") or "")

    def prune(self, path: str) -> None:
        """Delete the row at `path` (+ its FTS/vec rows) by unique identity."""
        self._memdb(["prune", "--path", path])

    def backup(self, keep: int = BACKUP_KEEP) -> str:
        """Copy the DB to <db>.bak-<utc>, retain the newest `keep`. Returns dst."""
        prog = (
            "import sys,shutil,glob,os,datetime;"
            "db=sys.argv[1];keep=int(sys.argv[2]);"
            "ts=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ');"
            "dst=db+'.bak-'+ts;"
            "(shutil.copy2(db,dst) if os.path.exists(db) else None);"
            "baks=sorted(glob.glob(db+'.bak-*'));"
            "([os.remove(p) for p in baks[:-keep]] if keep>0 and len(baks)>keep else None);"
            "print(dst)"
        )
        out = self._exec([self.py, "-c", prog, self.db, str(keep)])
        return out.strip()

    def write_base(self, content: str) -> None:
        """Write the agreed base manifest JSON to this endpoint's base path."""
        prog = "import sys;open(sys.argv[1],'w').write(sys.stdin.read())"
        self._exec([self.py, "-c", prog, self.base], input_text=content)


# --- Base manifest -----------------------------------------------------------


def read_base(path: Path) -> dict[str, str]:
    """Load {path: hash} from the base manifest; {} if absent (first run)."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("entries", {}))


def render_base(entries: dict[str, str]) -> str:
    return json.dumps(
        {
            "version": 1,
            "synced_at": _utc_now(),
            "entries": dict(sorted(entries.items())),
        },
        ensure_ascii=False, indent=2,
    )


def _utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- 3-way merge engine (pure) ----------------------------------------------

# Conflict subkinds drive the dialogue's option set + resolution semantics.
EDIT_EDIT = "edit_edit"          # both sides changed the same path differently
ADD_ADD = "add_add"              # both sides independently created the same path
DEL_LOCAL_EDIT_REMOTE = "del_local_edit_remote"   # local deleted, remote edited
DEL_REMOTE_EDIT_LOCAL = "del_remote_edit_local"   # remote deleted, local edited


@dataclass
class Action:
    op: str               # noop|push|pull|del_local|del_remote|conflict|base_clean
    path: str
    local: dict | None
    remote: dict | None
    base_hash: str | None
    kind: str | None = None      # conflict subkind (see constants above)
    reason: str = ""


def classify(local: dict[str, dict], remote: dict[str, dict],
             base: dict[str, str]) -> list[Action]:
    """Row-level 3-way merge -> ordered action list. Pure; no I/O."""
    actions: list[Action] = []
    for path in sorted(set(local) | set(remote) | set(base)):
        l = local.get(path)
        r = remote.get(path)
        b = base.get(path)
        lh = l["hash"] if l else None
        rh = r["hash"] if r else None

        def mk(op, kind=None, reason=""):
            return Action(op=op, path=path, local=l, remote=r, base_hash=b,
                          kind=kind, reason=reason)

        if l and r:
            if lh is None or rh is None:
                # A NULL stored hash makes the hash equality test unsafe
                # (None == None is True). Fall back to a direct content compare;
                # if still inconclusive, surface a conflict rather than a silent
                # no-op so the resolving upsert repairs the hash.
                same = (l.get("text") == r.get("text")
                        and l.get("name") == r.get("name")
                        and l.get("description") == r.get("description"))
                if same:
                    actions.append(mk("noop", reason="NULL hash; content identical"))
                else:
                    actions.append(mk("conflict", EDIT_EDIT,
                                      "NULL stored hash; content differs"))
            elif lh == rh:
                actions.append(mk("noop", reason="identical on both sides"))
            elif b is None:
                actions.append(mk("conflict", ADD_ADD,
                                  "same path created on both sides, different content"))
            elif lh == b:
                actions.append(mk("pull", reason="remote changed, local at base"))
            elif rh == b:
                actions.append(mk("push", reason="local changed, remote at base"))
            else:
                actions.append(mk("conflict", EDIT_EDIT,
                                  "both sides edited since base"))
        elif l and not r:
            if b is None:
                actions.append(mk("push", reason="new local entry"))
            elif lh == b:
                actions.append(mk("del_local", reason="remote deleted; local at base"))
            else:
                actions.append(mk("conflict", DEL_REMOTE_EDIT_LOCAL,
                                  "remote deleted but local edited"))
        elif r and not l:
            if b is None:
                actions.append(mk("pull", reason="new remote entry"))
            elif rh == b:
                actions.append(mk("del_remote", reason="local deleted; remote at base"))
            else:
                actions.append(mk("conflict", DEL_LOCAL_EDIT_REMOTE,
                                  "local deleted but remote edited"))
        else:  # neither present, but in base
            actions.append(mk("base_clean", reason="deleted on both sides"))
    return actions


# --- Conflict resolution -----------------------------------------------------


def _content(row: dict | None) -> str | None:
    """Content lines that matter for a human diff: description + body text.

    `updated`/tier/type are deliberately excluded (they live in the item header
    and `updated` always differs, which would be pure diff noise)."""
    if row is None:
        return None
    desc = row.get("description") or ""
    return (f"description: {desc}\n\n" if desc else "") + (row.get("text") or "")


def _diff_block(old_text: str | None, new_text: str | None,
                minus_label: str, plus_label: str,
                indent: str = "    ", max_lines: int = 40) -> str:
    """Git-style unified diff with explicit side labels (− = minus, + = plus)."""
    old = (old_text or "").splitlines()
    new = (new_text or "").splitlines()
    body = [ln for ln in difflib.unified_diff(old, new, lineterm="")
            if not (ln.startswith("--- ") or ln.startswith("+++ "))]
    out = [f"{indent}--- {minus_label}", f"{indent}+++ {plus_label}"]
    if not body:
        out.append(f"{indent}(no line-level differences; metadata only)")
    else:
        out += [f"{indent}{ln}" for ln in body[:max_lines]]
        if len(body) > max_lines:
            out.append(f"{indent}… ({len(body) - max_lines} more diff line(s))")
    return "\n".join(out)


def _render_diff_for(a: "Action", L: str, R: str, indent: str = "    ") -> str:
    """Per-memory line diff for one action, destination/side made explicit."""
    if a.op == "push":          # copy L -> R
        return _diff_block(_content(a.remote), _content(a.local),
                           f"{R} now" if a.remote else f"{R}  (absent)",
                           f"{R} after — copied from {L}", indent)
    if a.op == "pull":          # copy R -> L
        return _diff_block(_content(a.local), _content(a.remote),
                           f"{L} now" if a.local else f"{L}  (absent)",
                           f"{L} after — copied from {R}", indent)
    if a.op == "del_local":     # remove from L
        return _diff_block(_content(a.local), "",
                           f"{L}  (will be removed)", "(deleted)", indent)
    if a.op == "del_remote":    # remove from R
        return _diff_block(_content(a.remote), "",
                           f"{R}  (will be removed)", "(deleted)", indent)
    if a.op == "conflict":
        return _diff_block(_content(a.local), _content(a.remote),
                           f"{L}  (local)", f"{R}  (remote)", indent)
    return ""


def _action_label(a: "Action", L: str, R: str) -> str:
    """Plain, non-git headline for one plan item."""
    if a.op == "push":
        return f"{'NEW' if a.remote is None else 'UPDATE'}  ·  copy {L} → {R}"
    if a.op == "pull":
        return f"{'NEW' if a.local is None else 'UPDATE'}  ·  copy {R} → {L}"
    if a.op == "del_local":
        return f"DELETE from {L}  (it was removed on {R})"
    if a.op == "del_remote":
        return f"DELETE from {R}  (it was removed on {L})"
    if a.op == "conflict":
        return f"CONFLICT  ·  same entry differs on {L} and {R}"
    return a.op


def _options_for(kind: str, L: str, R: str) -> str:
    if kind in (EDIT_EDIT, ADD_ADD):
        return (f"[w]={L} (local)   [t]={R} (remote)   "
                f"[b]=both (keep each, rename one)   [s]=skip   [q]=quit")
    if kind == DEL_REMOTE_EDIT_LOCAL:   # R deleted it, L edited it
        return (f"[w]=keep the {L} edit (restore on {R})   "
                f"[t]=accept {R}'s delete (remove from {L})   [s]=skip   [q]=quit")
    # DEL_LOCAL_EDIT_REMOTE: L deleted it, R edited it
    return (f"[w]=accept {L}'s delete (remove from {R})   "
            f"[t]=keep the {R} edit (restore on {L})   [s]=skip   [q]=quit")


def resolve_conflicts(conflicts: list[Action], *, auto: str | None,
                      local_label: str, remote_label: str,
                      assume_yes: bool) -> dict[str, str]:
    """Return {path: choice} where choice in {local,remote,both,skip}.

    auto in {newer,local,remote} resolves non-interactively. Otherwise prompts
    per conflict on a tty; aborts (raises SyncAbort) on 'q'.
    """
    resolutions: dict[str, str] = {}
    if not conflicts:
        return resolutions

    if auto:
        for a in conflicts:
            if auto == "local":
                choice = "local"
            elif auto == "remote":
                choice = "remote"
            else:  # newer
                lu = (a.local or {}).get("updated") or ""
                ru = (a.remote or {}).get("updated") or ""
                # A deletion has no row; treat the surviving edited side as newer.
                if a.local is None:
                    choice = "remote"
                elif a.remote is None:
                    choice = "local"
                else:
                    choice = "local" if lu >= ru else "remote"
            resolutions[a.path] = choice
        return resolutions

    if not sys.stdin.isatty():
        raise SyncAbort(
            f"{len(conflicts)} conflict(s) need resolution but stdin is not a "
            f"tty — re-run with --auto {{newer|local|remote}} or on a terminal."
        )

    L, R = local_label, remote_label
    print(f"\n{len(conflicts)} memory conflict(s) — the same entry differs on "
          f"{L} (local) and {R} (remote).")
    print(f"In each diff:  '−' line = {L} (local)   '+' line = {R} (remote).")
    for i, a in enumerate(conflicts, 1):
        row = a.local or a.remote
        bar = "─" * 66
        print(f"\n{bar}")
        print(f"  CONFLICT [{i}/{len(conflicts)}]   memory: {a.path}")
        print(f"  name: {row.get('name')}    tier: {row.get('tier')}    "
              f"({a.reason})")
        print(bar)
        print(_render_diff_for(a, L, R))
        valid = {"w", "t", "s", "q"} | (
            {"b"} if a.kind in (EDIT_EDIT, ADD_ADD) else set())
        while True:
            print(f"\n  keep which?\n    {_options_for(a.kind, L, R)}")
            ans = input("  > ").strip().lower()
            if ans in valid:
                break
            print("    (invalid choice)")
        if ans == "q":
            raise SyncAbort("quit at conflict resolution — nothing applied")
        resolutions[a.path] = {"w": "local", "t": "remote",
                               "b": "both", "s": "skip"}[ans]
    return resolutions


# --- Action + resolution -> concrete ops -------------------------------------


@dataclass
class Op:
    role: str    # 'local' | 'remote'
    kind: str    # 'upsert' | 'prune'
    path: str
    row: dict | None = None


@dataclass
class Plan:
    ops: list[Op] = field(default_factory=list)
    skipped: set[str] = field(default_factory=set)
    extra_paths: set[str] = field(default_factory=set)   # 'both' suffixed adds
    summary: dict[str, int] = field(default_factory=dict)


def _suffix_row(row: dict, peer_label: str, taken: set[str]) -> dict:
    """A renamed copy of `row` so both divergent versions can coexist."""
    sfx = f"-conflict-{peer_label}"
    name = (row.get("name") or "entry") + sfx
    path = row["path"]
    new_path = (path[:-3] + sfx + ".md") if path.endswith(".md") else path + sfx
    n = 1
    while new_path in taken:
        n += 1
        new_path = (path[:-3] + f"{sfx}-{n}.md") if path.endswith(".md") else f"{path}{sfx}-{n}"
    out = dict(row)
    out["name"] = name
    out["path"] = new_path
    return out


def build_plan(actions: list[Action], resolutions: dict[str, str],
               *, peer_label: str, local: dict[str, dict],
               remote: dict[str, dict]) -> Plan:
    """Translate classified actions + conflict choices into endpoint ops."""
    plan = Plan()
    taken = set(local) | set(remote)
    counts = {k: 0 for k in
              ("push", "pull", "del_local", "del_remote", "conflict",
               "noop", "base_clean", "skip")}

    for a in actions:
        if a.op == "noop" or a.op == "base_clean":
            counts[a.op] += 1
            continue
        if a.op == "push":
            plan.ops.append(Op("remote", "upsert", a.path, a.local))
            counts["push"] += 1
        elif a.op == "pull":
            plan.ops.append(Op("local", "upsert", a.path, a.remote))
            counts["pull"] += 1
        elif a.op == "del_local":
            plan.ops.append(Op("local", "prune", a.path))
            counts["del_local"] += 1
        elif a.op == "del_remote":
            plan.ops.append(Op("remote", "prune", a.path))
            counts["del_remote"] += 1
        elif a.op == "conflict":
            counts["conflict"] += 1
            choice = resolutions.get(a.path)
            if choice is None:
                continue  # unresolved (e.g. dry-run preview) — no op, not a skip
            _resolve_to_ops(plan, a, choice, peer_label, taken, counts)
    plan.summary = counts
    return plan


def _resolve_to_ops(plan: Plan, a: Action, choice: str, peer_label: str,
                    taken: set[str], counts: dict[str, int]) -> None:
    if choice == "skip":
        plan.skipped.add(a.path)
        counts["skip"] += 1
        return

    if a.kind in (EDIT_EDIT, ADD_ADD):
        if choice == "local":
            plan.ops.append(Op("remote", "upsert", a.path, a.local))
        elif choice == "remote":
            plan.ops.append(Op("local", "upsert", a.path, a.remote))
        elif choice == "both":
            # Keep local at its path on both sides; preserve the remote version
            # under a suffixed path on both sides.
            sfx = _suffix_row(a.remote, peer_label, taken)
            taken.add(sfx["path"])
            plan.extra_paths.add(sfx["path"])
            plan.ops.append(Op("remote", "upsert", a.path, a.local))
            plan.ops.append(Op("local", "upsert", sfx["path"], sfx))
            plan.ops.append(Op("remote", "upsert", sfx["path"], sfx))
        return

    # delete-vs-edit: 'local'/'remote' name the WINNING side's intent.
    if a.kind == DEL_REMOTE_EDIT_LOCAL:   # remote deleted, local edited
        if choice == "local":             # keep the local edit -> restore remote
            plan.ops.append(Op("remote", "upsert", a.path, a.local))
        else:                              # honor the remote delete -> drop local
            plan.ops.append(Op("local", "prune", a.path))
    elif a.kind == DEL_LOCAL_EDIT_REMOTE:  # local deleted, remote edited
        if choice == "remote":            # keep the remote edit -> restore local
            plan.ops.append(Op("local", "upsert", a.path, a.remote))
        else:                              # honor the local delete -> drop remote
            plan.ops.append(Op("remote", "prune", a.path))


# --- Plan rendering ----------------------------------------------------------


def print_plan(actions: list[Action], plan: Plan, *,
               local_label: str, remote_label: str) -> None:
    L, R = local_label, remote_label
    s = plan.summary
    moved = [a for a in actions if a.op not in ("noop", "base_clean")]
    bar = "═" * 70
    print(f"\n{bar}")
    print(f"  MEMORY SYNC — plan        local = {L}        remote = {R}")
    print(bar)
    if not moved:
        # 'everything already in sync' is a stable sentinel other tools grep for.
        print("  everything already in sync — nothing to do.")
        print(f"{bar}\n")
        return
    for i, a in enumerate(moved, 1):
        row = a.local or a.remote
        print(f"\n──[{i}]──  {_action_label(a, L, R)}")
        print(f"     memory:  {a.path}")
        print(f"     name:    {row.get('name')}     tier: {row.get('tier')}")
        print(_render_diff_for(a, L, R, indent="     "))
    dels = s["del_local"] + s["del_remote"]
    skipped = f",  {s['skip']} skipped" if s["skip"] else ""
    print(f"\n{bar}")
    print(f"  summary: {len(moved)} change(s) —  "
          f"{s['push']} copy → {R},  {s['pull']} copy → {L},  "
          f"{dels} delete(s),  {s['conflict']} conflict(s){skipped}"
          f"   ·   {s['noop']} unchanged")
    print(f"  ({len(plan.ops)} memory-row operation(s) to apply)")
    print(f"{bar}\n")


# --- Apply -------------------------------------------------------------------


def apply_plan(plan: Plan, *, local_ep: Endpoint, remote_ep: Endpoint,
               old_base: dict[str, str], backup_keep: int,
               verify: bool) -> dict[str, str]:
    """Execute ops with backups + idempotent ordering; return the new base map."""
    role = {"local": local_ep, "remote": remote_ep}

    # 1. Back up BOTH sides before any mutation. Skip when there is nothing to
    #    apply so a no-op login sync does not churn the backup rotation.
    if plan.ops:
        print("  backing up both DBs ...")
        lb = local_ep.backup(backup_keep)
        rb = remote_ep.backup(backup_keep)
        print(f"    local  -> {lb}")
        print(f"    remote -> {rb}")

    # 2. Upserts first, deletes last -> an interrupted run re-converges.
    upserts = [o for o in plan.ops if o.kind == "upsert"]
    prunes = [o for o in plan.ops if o.kind == "prune"]
    for o in upserts:
        role[o.role].upsert(o.row)
    for o in prunes:
        role[o.role].prune(o.path)
    if plan.ops:
        print(f"  applied {len(upserts)} upsert(s), {len(prunes)} prune(s).")

    # 3. Rebuild the base from a POST-APPLY local re-export (authoritative stored
    #    hashes) — never from a hash we compute. Skipped conflicts keep their old
    #    base entry so they are re-detected next run.
    local_after = local_ep.export(with_body=False)
    new_base: dict[str, str] = {}
    for path, row in local_after.items():
        if path in plan.skipped:
            continue
        new_base[path] = row["hash"]
    for path in plan.skipped:
        if path in old_base:
            new_base[path] = old_base[path]

    base_json = render_base(new_base)
    local_ep.write_base(base_json)
    try:
        remote_ep.write_base(base_json)
    except EndpointError as exc:
        print(f"  WARN: could not mirror base manifest to remote: {exc}")

    # 4. Convergence verify: re-export remote, confirm agreed paths match.
    if verify:
        remote_after = remote_ep.export(with_body=False)
        agreed = (set(local_after) & set(remote_after)) - plan.skipped
        residual = [p for p in agreed
                    if local_after[p]["hash"] != remote_after[p]["hash"]]
        only_local = set(local_after) - set(remote_after) - plan.skipped
        only_remote = set(remote_after) - set(local_after) - plan.skipped
        if residual or only_local or only_remote:
            print(f"  VERIFY: {len(agreed) - len(residual)} path(s) identical; "
                  f"residual-hash-diff={len(residual)} "
                  f"only-local={len(only_local)} only-remote={len(only_remote)}")
            if residual:
                print("    (residual hash diffs — typically a legacy pre-current-hash "
                      "row; self-heals on its next edit):")
                for p in residual[:10]:
                    print(f"      {p}")
        else:
            print(f"  VERIFY: converged — {len(agreed)} path(s) identical on both sides.")
    return new_base


# --- Lockfile ----------------------------------------------------------------


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        # Retry once after reclaiming a stale lock; if a concurrent process wins
        # the reclaim race, surface a clean SkipSync (not an unhandled traceback).
        for attempt in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if attempt == 0 and self._stale():
                    try:
                        os.unlink(self.path)
                    except FileNotFoundError:
                        pass  # another process reclaimed it first; retry create
                else:
                    raise SkipSync(f"another sync is running (lock: {self.path})")
        os.write(self.fd, f"{os.getpid()} {_utc_now()}\n".encode())
        return self

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text().split()[0])
        except (ValueError, IndexError, OSError):
            return True
        try:
            os.kill(pid, 0)
            return False   # process alive
        except ProcessLookupError:
            return True
        except PermissionError:
            return False   # exists, owned by someone else

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass


# --- CLI ---------------------------------------------------------------------


def build_endpoints(ns) -> tuple[Endpoint, Endpoint]:
    local = Endpoint(
        "local", py=str(Path(ns.local_py).expanduser()),
        script=str(Path(ns.script).expanduser()),
        db=str(Path(ns.local_db).expanduser()),
        base=str(Path(ns.base).expanduser()),
    )
    if ns.peer_host:
        home = _remote_home(ns.peer_host, ns.ssh_opt)
        peer_db = ns.peer_db or f"{home}/{REL_DB}"
        peer = Endpoint(
            ns.peer_host, py=f"{home}/{REL_PY}", script=f"{home}/{REL_SCRIPT}",
            db=peer_db, base=f"{home}/{REL_BASE}",
            ssh_host=ns.peer_host, ssh_opts=ns.ssh_opt or DEFAULT_SSH_OPTS,
        )
    else:
        if not ns.peer_db:
            raise SyncAbort("need --peer-host HOST (ssh) or --peer-db PATH (local).")
        peer = Endpoint(
            "peer-local", py=str(Path(ns.local_py).expanduser()),
            script=str(Path(ns.script).expanduser()),
            db=str(Path(ns.peer_db).expanduser()),
            base=str(Path(ns.peer_db).expanduser()) + ".sync-base.json",
        )
    return local, peer


def _remote_home(host: str, ssh_opts: list[str] | None) -> str:
    opts = ssh_opts or DEFAULT_SSH_OPTS
    try:
        out = subprocess.run(["ssh", *opts, host, 'echo "$HOME"'],
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=12)
    except subprocess.TimeoutExpired as exc:
        raise SkipSync(f"peer {host!r} unreachable (timeout resolving $HOME)") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise SkipSync(f"peer {host!r} unreachable: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="claude-mem-sync",
        description="Cross-device 3-way reconciliation of the .memory.db store.",
    )
    p.add_argument("--peer-host", metavar="HOST",
                   help="SSH host of the peer (e.g. peer). Omit for a local-local test.")
    p.add_argument("--peer-db", metavar="PATH",
                   help="Peer DB path (default: peer ~/.claude/agent-memory/.memory.db; "
                        "for --peer-host omitted, this is a second local DB).")
    p.add_argument("--local-db", default=str(DEFAULT_DB), metavar="PATH")
    p.add_argument("--local-py", default=str(DEFAULT_PY), metavar="PATH")
    p.add_argument("--script", default=str(DEFAULT_SCRIPT), metavar="PATH")
    p.add_argument("--base", default=str(DEFAULT_BASE), metavar="PATH",
                   help="Local base manifest path.")
    p.add_argument("--ssh-opt", action="append", dest="ssh_opt", default=None,
                   metavar="OPT", help="Extra ssh -o option (repeatable).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; mutate nothing.")
    p.add_argument("--auto", choices=["newer", "local", "remote"],
                   help="Resolve conflicts non-interactively.")
    p.add_argument("--yes", action="store_true",
                   help="Apply without the final confirmation prompt.")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip the post-apply convergence re-export.")
    p.add_argument("--backup-keep", type=int, default=BACKUP_KEEP, metavar="N")
    p.add_argument("--local-label", default="WSL", metavar="NAME",
                   help="Display name for the local side (default: WSL).")
    p.add_argument("--remote-label", default=None, metavar="NAME",
                   help="Display name for the peer side (default: the peer host).")
    ns = p.parse_args(argv)

    try:
        local_ep, peer_ep = build_endpoints(ns)
        local_ep.probe()
        peer_ep.probe()

        local_label = ns.local_label
        remote_label = ns.remote_label or ns.peer_host or "peer"

        if ns.dry_run:
            # Read-only: no lock. Only auto-resolutions are shown; an interactive
            # prompt would imply intent to apply.
            local = local_ep.export(with_body=True)
            remote = peer_ep.export(with_body=True)
            base = read_base(Path(ns.base).expanduser())
            actions = classify(local, remote, base)
            conflicts = [a for a in actions if a.op == "conflict"]
            res = resolve_conflicts(conflicts, auto=ns.auto,
                                    local_label=local_label,
                                    remote_label=remote_label, assume_yes=True) \
                if ns.auto else {}
            plan = build_plan(actions, res, peer_label=remote_label,
                              local=local, remote=remote)
            print_plan(actions, plan, local_label=local_label,
                       remote_label=remote_label)
            if conflicts and not ns.auto:
                print(f"  ({len(conflicts)} conflict(s) would prompt interactively; "
                      f"re-run without --dry-run or with --auto.)")
            return 0

        # Apply path: hold the lock across export -> classify -> resolve -> apply
        # so a concurrent run cannot classify the same snapshot and then apply a
        # stale plan on top of an already-mutated DB (a second run skips cleanly).
        # The lock is per-local-DB so distinct DB pairs (incl. test harnesses)
        # never contend.
        lock_path = Path(str(Path(ns.local_db).expanduser()) + ".sync.lock")
        with Lock(lock_path):
            local = local_ep.export(with_body=True)
            remote = peer_ep.export(with_body=True)
            base = read_base(Path(ns.base).expanduser())
            actions = classify(local, remote, base)
            conflicts = [a for a in actions if a.op == "conflict"]

            res = resolve_conflicts(conflicts, auto=ns.auto,
                                    local_label=local_label,
                                    remote_label=remote_label, assume_yes=ns.yes)
            plan = build_plan(actions, res, peer_label=remote_label,
                              local=local, remote=remote)
            print_plan(actions, plan, local_label=local_label,
                       remote_label=remote_label)

            if plan.ops and not ns.yes:
                if not sys.stdin.isatty():
                    raise SyncAbort("not a tty — pass --yes to apply non-interactively.")
                if input("  apply this plan? [y/N] ").strip().lower() not in ("y", "yes"):
                    raise SyncAbort("declined — nothing applied.")

            apply_plan(plan, local_ep=local_ep, remote_ep=peer_ep,
                       old_base=base, backup_keep=ns.backup_keep,
                       verify=not ns.no_verify)
        print("  sync complete.")
        return 0

    except SkipSync as exc:
        print(f"claude-mem-sync: SKIP — {exc}", file=sys.stderr)
        return 0
    except SyncAbort as exc:
        print(f"claude-mem-sync: ABORT — {exc}", file=sys.stderr)
        return 2
    except EndpointError as exc:
        print(f"claude-mem-sync: ENDPOINT ERROR — {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
