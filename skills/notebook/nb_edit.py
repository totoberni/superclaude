"""/notebook skill — batch edit + diff + find + revert.

Atomic multi-cell mutation. All ops apply or none do.

V1.1 — see example-project-review-of-skill-v1.md for fix list (V1-H8/H9/H19/H20/H21/M2/M4/M5/L1/L5/L6/L7).

V1.2 — addresses example-project-review-of-skill-v2.md issues:
  - V2-H2: `cmd_revert` mirrors `cmd_batch` safety chain (JL check pre+post lock,
           SHA capture inside lock, paired-.py re-sync after restore).
  - V2-M1: snapshot failures raise `SnapshotError` (was caught as plain OSError).
  - V2-M2: snapshot is atomic (tempfile + os.replace) and uses microsecond +
           short UUID suffix to avoid same-second collisions.
  - V2-M3: `nb revert --target <nb>` flag for explicit target; default still
           derives from `<.notebook/snapshots/>` convention.
  - V2-M4: all `sys.exit(...)` sites in nb_edit converted to typed `SkillError`
           subclasses; CLI translates at the boundary in `nb_main.py`.
  - V2-L5: dead `--in` validation branch in `cmd_find` removed (argparse
           `choices=` already validates).
  - V2-L7: pre-lock JL-detector kept as "early-fail optimisation" with explicit
           comment; post-lock check is the TOCTOU-safe one.
"""
from __future__ import annotations

import copy
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import nbformat

from nb_io import (
    LockPathError,
    NotebookConflict,
    assert_git_clean_or_force,
    assert_no_jl_writer,
    atomic_write_ipynb,
    load_ipynb,
    notebook_lock,
    sha256_file,
)

# V1-L7: The `# %%` literal rule.
_PCT_LITERAL_RE = re.compile(r"^\s*#\s*%%", re.MULTILINE)
# V1-M5: nbformat 4.5+ cell-id regex.
_CELL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Typed exceptions (V1-L5 + V2-M4: full unification at CLI boundary).
# ---------------------------------------------------------------------------


class SkillError(RuntimeError):
    """Base class for /notebook user-visible errors. Translated to exit 1
    (or specific code) at the dispatcher boundary in `nb_main.py`."""


class PlanError(SkillError, ValueError):
    """Bad plan structure or unresolvable target cell."""


class SnapshotError(SkillError):
    """Snapshot create / restore failed."""


class FindError(SkillError):
    """`nb find` could not produce a useful answer."""


class RevertError(SkillError):
    """`nb revert` could not resolve target or restore."""


class DepError(SkillError):
    """A required Python dependency is missing."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError as e:
        raise DepError(
            "PyYAML required for plan files. `pip install PyYAML`."
        ) from e


def _load_plan(plan_path: Path) -> dict:
    yaml = _import_yaml()
    with plan_path.open("r", encoding="utf-8") as f:
        plan = yaml.safe_load(f)
    if not isinstance(plan, dict) or "operations" not in plan:
        raise PlanError(f"plan at {plan_path} must be a YAML mapping with key 'operations'")
    return plan


def _new_cell_id() -> str:
    """JEP-62-compliant: random-UUID, 8 chars, alphanumeric. Stable post-creation."""
    return uuid.uuid4().hex[:8]


def _ensure_id(cell: dict) -> dict:
    """V1-M5: if existing id is non-conformant to nbformat 4.5+ regex, regenerate."""
    cur = cell.get("id")
    if not cur or not _CELL_ID_RE.match(cur):
        cell["id"] = _new_cell_id()
    return cell


def _validate_no_pct_literal(source: str, where: str) -> None:
    if _PCT_LITERAL_RE.search(source):
        raise PlanError(
            f"{where}: source contains a line starting with `# %%` "
            "(any indentation). This silently splits cells via jupytext py:percent. "
            "Choose any of: rewrite the comment text, change to a different prefix, "
            "or move the literal to a markdown cell. (Indenting does NOT help — "
            "the regex is anchored to start-of-line with optional leading whitespace.)"
        )


def _resolve_target(nb: dict, op: dict, op_idx: int) -> int:
    cells = nb["cells"]
    keys = ("cell_id", "cell_tag", "at_position", "before_id", "after_id")
    found = [k for k in keys if k in op]
    if len(found) != 1:
        raise PlanError(f"op[{op_idx}] {op.get('op')!r}: exactly one of {keys} required, got {found}")
    key = found[0]
    val = op[key]
    op_kind = op.get("op")
    if key == "at_position":
        if not isinstance(val, int):
            raise PlanError(f"op[{op_idx}]: at_position must be int, got {type(val).__name__}")
        if op_kind == "insert":
            if not (0 <= val <= len(cells)):
                raise PlanError(f"op[{op_idx}] insert: at_position={val} out of range [0, {len(cells)}]")
        else:
            if not (0 <= val < len(cells)):
                raise PlanError(f"op[{op_idx}] {op_kind}: at_position={val} out of range [0, {len(cells)})")
        return val
    if key == "cell_id":
        for i, c in enumerate(cells):
            if c.get("id") == val:
                return i
        raise PlanError(f"op[{op_idx}]: cell_id={val!r} not found")
    if key == "cell_tag":
        for i, c in enumerate(cells):
            if val in c.get("metadata", {}).get("tags", []):
                return i
        raise PlanError(f"op[{op_idx}]: cell_tag={val!r} not found")
    if key in ("before_id", "after_id"):
        for i, c in enumerate(cells):
            if c.get("id") == val:
                return i if key == "before_id" else i + 1
        raise PlanError(f"op[{op_idx}]: {key}={val!r} not found")
    raise AssertionError("unreachable")


def _build_cell(op: dict, op_idx: int) -> dict:
    cell_type = op.get("cell_type")
    if cell_type not in ("code", "markdown", "raw"):
        raise PlanError(f"op[{op_idx}]: cell_type must be code|markdown|raw, got {cell_type!r}")
    source = op.get("source", op.get("new_source", ""))
    if cell_type == "code":
        _validate_no_pct_literal(source, f"op[{op_idx}] source")
    cell: dict[str, Any] = {
        "cell_type": cell_type,
        "id": _new_cell_id(),
        "metadata": {},
        "source": _canonicalize_source(source),
    }
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    if "tags" in op:
        cell["metadata"]["tags"] = list(op["tags"])
    if "metadata" in op:
        cell["metadata"].update(op["metadata"])
    return cell


def _canonicalize_source(s: str) -> list[str]:
    """nbformat list-of-strings convention: each element ends in \\n except last.
    V1-M4: CRLF and bare CR normalised to LF."""
    if not s:
        return []
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = s.split("\n")
    out = [ln + "\n" for ln in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


def apply_plan(nb: dict, plan: dict) -> dict:
    """Apply all ops to nb in-memory. Returns the mutated nb (same object)."""
    cells = nb["cells"]
    for c in cells:
        _ensure_id(c)
    for op_idx, op in enumerate(plan["operations"]):
        kind = op.get("op")
        if kind == "replace":
            tgt = _resolve_target(nb, op, op_idx)
            new_src = op.get("new_source", op.get("source"))
            if new_src is None:
                raise PlanError(f"op[{op_idx}] replace: missing new_source")
            cell = cells[tgt]
            if cell["cell_type"] == "code":
                _validate_no_pct_literal(new_src, f"op[{op_idx}] new_source")
            cell["source"] = _canonicalize_source(new_src)
            keep_output = (
                op.get("metadata", {}).get("keep_output", False)
                or cell.get("metadata", {}).get("keep_output", False)
            )
            if cell["cell_type"] == "code" and not keep_output:
                cell["execution_count"] = None
                cell["outputs"] = []
                cid = cell.get("id", "?")
                print(f"[notebook] cell {cid}: outputs cleared (op:replace; "
                      "set metadata.keep_output=true to preserve)",
                      file=sys.stderr)
            if "tags" in op:
                cell.setdefault("metadata", {})["tags"] = list(op["tags"])
            if "metadata" in op:
                cell.setdefault("metadata", {}).update(op["metadata"])
        elif kind == "insert":
            tgt = _resolve_target(nb, op, op_idx)
            cells.insert(tgt, _build_cell(op, op_idx))
        elif kind == "insert_block":
            # UX-1: insert N cells after a single anchor in directive order.
            # Reverse-iterate so each cell is inserted with the SAME anchor;
            # each prior insert pushes later ones forward, yielding final
            # order = directive order. (This is the manual workaround orchs
            # were doing by hand — implemented once, here.)
            sub_cells = op.get("cells")
            if not isinstance(sub_cells, list) or not sub_cells:
                raise PlanError(
                    f"op[{op_idx}] insert_block: `cells` must be a non-empty list"
                )
            # Validate anchor: insert_block requires exactly one positional
            # selector (cell_id / cell_tag / at_position / before_id / after_id).
            keys = ("cell_id", "cell_tag", "at_position", "before_id", "after_id")
            anchor_keys = [k for k in keys if k in op]
            if len(anchor_keys) != 1:
                raise PlanError(
                    f"op[{op_idx}] insert_block: exactly one of {keys} required, "
                    f"got {anchor_keys}"
                )
            anchor_key = anchor_keys[0]
            anchor_val = op[anchor_key]
            # Reverse-iterate: last cell inserted first at the anchor;
            # subsequent (earlier) inserts at the same anchor push it forward.
            for sub_idx, sub in enumerate(reversed(sub_cells)):
                if not isinstance(sub, dict):
                    raise PlanError(
                        f"op[{op_idx}] insert_block: cells[{len(sub_cells) - 1 - sub_idx}] "
                        f"must be a mapping, got {type(sub).__name__}"
                    )
                # Translate `type` → `cell_type` so _build_cell accepts it.
                sub_op: dict[str, Any] = {
                    "op": "insert",
                    anchor_key: anchor_val,
                    "cell_type": sub.get("cell_type", sub.get("type")),
                    "source": sub.get("source", ""),
                }
                if "tags" in sub:
                    sub_op["tags"] = sub["tags"]
                if "metadata" in sub:
                    sub_op["metadata"] = sub["metadata"]
                tgt = _resolve_target(nb, sub_op, op_idx)
                new_cell = _build_cell(sub_op, op_idx)
                # Honour user-provided stable id if present (UX-1 edge case).
                user_id = sub.get("id")
                if user_id:
                    if not _CELL_ID_RE.match(user_id):
                        raise PlanError(
                            f"op[{op_idx}] insert_block: cells[{len(sub_cells) - 1 - sub_idx}] "
                            f"id={user_id!r} fails nbformat 4.5+ regex"
                        )
                    new_cell["id"] = user_id
                cells.insert(tgt, new_cell)
        elif kind == "delete":
            tgt = _resolve_target(nb, op, op_idx)
            del cells[tgt]
        elif kind == "reorder":
            new_order = op.get("new_order")
            if not isinstance(new_order, list):
                raise PlanError(f"op[{op_idx}] reorder: new_order must be a list of cell IDs")
            id_to_cell = {c["id"]: c for c in cells}
            missing = set(id_to_cell) - set(new_order)
            extra = set(new_order) - set(id_to_cell)
            if missing or extra:
                raise PlanError(
                    f"op[{op_idx}] reorder: missing IDs={sorted(missing)} extra IDs={sorted(extra)}"
                )
            cells[:] = [id_to_cell[i] for i in new_order]
        else:
            raise PlanError(f"op[{op_idx}]: unknown op {kind!r}")
    return nb


# ---------------------------------------------------------------------------
# Snapshot / revert (V1-H19 + V2-M1/M2/M3/H2)
# ---------------------------------------------------------------------------


def _snapshot_dir(notebook: Path) -> Path:
    return notebook.parent / ".notebook" / "snapshots"


def take_snapshot(notebook: Path) -> Path:
    """V2-M2: atomic copy via tempfile + os.replace; microsecond + uuid-6 suffix
    to avoid same-second collisions. V2-M1: raises SnapshotError on failure."""
    sd = _snapshot_dir(notebook)
    try:
        sd.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SnapshotError(f"could not create snapshot dir {sd}: {e}") from e
    ts = time.strftime("%Y%m%dT%H%M%S")
    micros = f"{int((time.time() % 1) * 1_000_000):06d}"
    suffix = uuid.uuid4().hex[:6]
    dest = sd / f"{notebook.stem}-{ts}.{micros}-{suffix}.ipynb"
    try:
        # tempfile + os.replace = atomic snapshot.
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(sd),
            prefix=f".{dest.name}.",
            suffix=".tmp",
        )
        os.close(tmp_fd)
        shutil.copy2(notebook, tmp_path)
        os.replace(tmp_path, dest)
    except OSError as e:
        # Best-effort cleanup of the tempfile; ignore if already moved.
        try:
            Path(tmp_path).unlink()
        except (OSError, NameError):
            pass
        raise SnapshotError(f"snapshot of {notebook} → {dest} failed: {e}") from e
    return dest


def _resolve_revert_target(snapshot: Path, override: Path | None) -> Path:
    """V2-M3: `--target` overrides the convention-based resolution."""
    if override is not None:
        target = override.resolve()
        if not target.exists():
            raise RevertError(f"--target {target} not found.")
        return target
    # Convention: <project>/.notebook/snapshots/<stem>-<ts>.<micros>-<uuid>.ipynb
    nb_dir = snapshot.parent.parent.parent
    stem_with_ts = snapshot.stem
    # New format: stem-YYYYMMDDTHHMMSS.MICROS-UUID6 → strip after last `-` (uuid),
    # then strip after last `-` (ts.micros).
    parts = stem_with_ts.rsplit("-", 2)  # ['stem', 'YYYYMMDDTHHMMSS.MICROS', 'UUID']
    if len(parts) >= 3:
        stem = parts[0]
    else:
        # Old format fallback: stem-YYYYMMDDTHHMMSS
        stem = stem_with_ts.rsplit("-", 1)[0]
    target = nb_dir / f"{stem}.ipynb"
    if not target.exists():
        raise RevertError(
            f"target notebook {target} not found; cannot revert. "
            "Pass `--target <nb>` to override the path resolution."
        )
    return target


def cmd_revert(args) -> int:
    """V2-H2: full safety chain — JL check (pre+post lock), SHA capture inside
    lock, atomic write, paired-.py re-sync after restore.
    V2-M3: `--target` flag for explicit target.
    """
    snap: Path = args.snapshot.resolve()
    if not snap.exists():
        raise RevertError(f"snapshot not found: {snap}")
    target = _resolve_revert_target(snap, getattr(args, "target", None))

    # V2-H2 safety chain mirrors cmd_batch.
    if not getattr(args, "force_no_jl_check", False):
        try:
            assert_no_jl_writer(target, baseline_mtime=target.stat().st_mtime)
        except NotebookConflict as e:
            raise RevertError(str(e)) from e
    try:
        with notebook_lock(target, timeout=getattr(args, "lock_timeout", 30.0)):
            if not getattr(args, "force_no_jl_check", False):
                assert_no_jl_writer(target)
            _ = sha256_file(target)  # pre-state SHA captured (debug-only here)
            nb = load_ipynb(snap)
            atomic_write_ipynb(target, nb)
    except (NotebookConflict, LockPathError, TimeoutError) as e:
        raise RevertError(str(e)) from e

    # V2-H2: re-sync paired .py from the restored .ipynb so the next
    # jupytext --sync doesn't overwrite the freshly-reverted .ipynb from a
    # stale .py.
    try:
        from nb_jupytext import sync_from_ipynb
        sync_from_ipynb(target)
    except Exception as e:  # noqa: BLE001
        print(f"[notebook] paired .py re-sync skipped: {e}", file=sys.stderr)

    print(f"[notebook] reverted {target} from snapshot {snap}")
    return 0


# ---------------------------------------------------------------------------
# Find (V1-H20)
# ---------------------------------------------------------------------------


def cmd_find(args) -> int:
    """`nb find <nb> <pattern> --in source|tags|metadata` — list matched cell IDs.

    V2-L5: dropped the dead `else: sys.exit(...)` branch (argparse `choices=`
    already validates the `--in` value).
    """
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        raise FindError(f"not found: {nb_path}")
    pat = re.compile(args.pattern)
    where = args.where or "source"
    nb = nbformat.read(str(nb_path), as_version=4)
    hits: list[tuple[int, str, str]] = []
    for i, cell in enumerate(nb.cells):
        cid = cell.get("id") or "<no-id>"
        if where == "source":
            src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
            m = pat.search(src)
            if m:
                start = max(0, m.start() - 20)
                end = min(len(src), m.end() + 20)
                snippet = src[start:end].replace("\n", " ")
                hits.append((i, cid, snippet))
        elif where == "tags":
            for t in cell.get("metadata", {}).get("tags", []):
                if pat.search(t):
                    hits.append((i, cid, f"tag={t}"))
                    break
        elif where == "metadata":
            md = cell.get("metadata", {})
            md_str = repr(md)
            if pat.search(md_str):
                hits.append((i, cid, md_str[:80]))
    if not hits:
        print(f"[notebook] no matches for pattern {args.pattern!r} in {where}")
        return 1
    for idx, cid, snippet in hits:
        print(f"  cell[{idx}] id={cid}: {snippet}")
    return 0


# ---------------------------------------------------------------------------
# Batch + diff
# ---------------------------------------------------------------------------


def cmd_batch(args) -> int:
    """`nb batch <nb> --plan <plan.yml>` — atomic multi-cell mutation."""
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        raise PlanError(f"not found: {nb_path}")
    plan = _load_plan(args.plan)
    if args.dry_run:
        return _dry_run(nb_path, plan)
    try:
        assert_git_clean_or_force(nb_path, force=getattr(args, "force", False))
    except NotebookConflict as e:
        raise PlanError(str(e)) from e
    # V2-L7: pre-lock JL check is an EARLY-FAIL OPTIMISATION (cheap; saves a
    # lock acquire when JL has the file open). The post-lock check below is
    # the TOCTOU-safe authoritative one.
    if not args.force_no_jl_check:
        try:
            assert_no_jl_writer(nb_path, baseline_mtime=nb_path.stat().st_mtime)
        except NotebookConflict as e:
            raise PlanError(str(e)) from e
    snapshot_path: Path | None = None
    if getattr(args, "snapshot", False):
        snapshot_path = take_snapshot(nb_path)  # raises SnapshotError on failure
        print(f"[notebook] snapshot: {snapshot_path}", file=sys.stderr)
    try:
        with notebook_lock(nb_path, timeout=args.lock_timeout):
            if not args.force_no_jl_check:
                assert_no_jl_writer(nb_path)  # TOCTOU-safe re-check
            pre_sha = sha256_file(nb_path)
            nb = load_ipynb(nb_path)
            apply_plan(nb, plan)
            nbformat.validate(nb)
            if sha256_file(nb_path) != pre_sha:
                raise NotebookConflict(
                    "notebook changed between read and write inside lock; "
                    "concurrent writer with insufficient lock discipline detected."
                )
            atomic_write_ipynb(nb_path, nb)
    except NotebookConflict as e:
        raise PlanError(str(e)) from e
    except LockPathError as e:
        raise PlanError(str(e)) from e
    except TimeoutError as e:
        raise PlanError(str(e)) from e
    try:
        from nb_jupytext import sync_from_ipynb
        sync_from_ipynb(nb_path)
    except Exception as e:  # noqa: BLE001
        print(f"[notebook] sync skipped: {e}", file=sys.stderr)
    print(f"[notebook] batch applied: {len(plan['operations'])} op(s) on {nb_path}"
          + (f" (snapshot: {snapshot_path})" if snapshot_path else ""))
    return 0


def _normalise_sources(nb: dict) -> dict:
    """V3-X5: nbdime's `diff_strings_linewise` asserts source is `str`, but
    nbformat 5.x reads canonicalised list-form sources back as `list[str]`.
    Join them so the diff display path doesn't AssertionError. Caller is
    responsible for handing in a deepcopy — we MUST NOT mutate notebooks
    that flow into the write path.
    """
    for c in nb.get("cells", []):
        src = c.get("source")
        if isinstance(src, list):
            c["source"] = "".join(src)
    return nb


def _dry_run(nb_path: Path, plan: dict) -> int:
    nb = load_ipynb(nb_path)
    before = nbformat.from_dict(nb)
    nb_after = load_ipynb(nb_path)
    apply_plan(nb_after, plan)  # raises PlanError; dispatcher translates
    after = nbformat.from_dict(nb_after)
    try:
        from io import StringIO
        import nbdime
        from nbdime.prettyprint import pretty_print_notebook_diff, PrettyPrintConfig
    except ImportError as e:
        raise DepError(
            "nbdime required for --dry-run / diff. `pip install nbdime`."
        ) from e
    # V3-X5: normalise list-form sources on a deepcopy so nbdime's
    # diff_strings_linewise gets `str` (its assert), and the original
    # `before`/`after` (not actually re-used downstream, but defensive)
    # remain untouched.
    before_norm = _normalise_sources(copy.deepcopy(before))
    after_norm = _normalise_sources(copy.deepcopy(after))
    diff = nbdime.diff_notebooks(before_norm, after_norm)
    buf = StringIO()
    pretty_print_notebook_diff(
        str(nb_path), str(nb_path) + " (after)", before_norm, diff,
        config=PrettyPrintConfig(out=buf),
    )
    sys.stdout.write(buf.getvalue())
    return 0


def cmd_diff(args) -> int:
    """`nb diff <nb> --plan <plan.yml>` — preview as nbdime textual diff."""
    return _dry_run(args.notebook.resolve(), _load_plan(args.plan))


# `os` is used by atomic-snapshot; import lazily to avoid top-level shuffle.
import os  # noqa: E402
