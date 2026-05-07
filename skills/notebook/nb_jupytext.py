"""/notebook skill — jupytext sync helpers.

Wraps `jupytext --sync` with skill atomic-write. The .ipynb is authoritative
for cell IDs (per W2 empirical: jupytext.read of orphan .py randomises IDs;
only --sync against an existing .ipynb preserves IDs via content matching).

V1.1 — addresses example-project-review-of-skill-v1.md issues:
  - V1-H10: ID-loss detection — pre/post-sync ID-set diff with WARN/FAIL.
  - V1-H13: `# %%` literal check applied to .py before sync (was nb_edit-only).
  - V1-H14: jupytext stderr surfaced on CalledProcessError.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import nbformat

from nb_io import atomic_write_ipynb, notebook_lock

# V1-H13 + V2-L1: `# %%` literal in CODE cells silently splits on jupytext read.
# We can't perfectly distinguish in-cell from cell-marker without parsing, but
# we can whitelist the SHAPES jupytext considers valid cell markers and warn
# loudly on every other `^# %%` occurrence.
#
# Valid cell-marker shapes (jupytext py:percent format, all start of line):
#   - `# %%`               (bare; followed by end-of-line)
#   - `# %% [markdown]`    (cell type)
#   - `# %% [raw]`
#   - `# %% tags=["foo"]`  (key=value metadata)
#   - `# %% id="abc"`
# Any other `^# %%` is suspicious — likely an in-cell literal that will split.
_VALID_MARKER_RE = re.compile(
    r"^[ \t]*#\s*%%(?:\s*$|\s+\[|\s+\w+\s*=)",
    re.MULTILINE,
)
_ANY_PCT_RE = re.compile(r"^[ \t]*#\s*%%", re.MULTILINE)


def _paired_py(notebook: Path) -> Path:
    return notebook.with_suffix(".py")


def _ipynb_cell_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        nb = nbformat.read(str(path), as_version=4)
    except Exception:  # noqa: BLE001
        return set()
    return {c.get("id") for c in nb.cells if c.get("id")}


def _check_pct_literal_in_py(py: Path) -> None:
    """V2-L1: actually warn (not no-op).

    Compares total `^# %%` count vs valid-marker count; the difference is the
    suspect-literal count. For each suspect line, prints filename:lineno + the
    line itself for diagnostic. Soft warning, not hard-fail (false positives
    are possible — e.g. legitimate markdown content with `# %%` text)."""
    text = py.read_text(encoding="utf-8")
    valid_count = len(_VALID_MARKER_RE.findall(text))
    any_count = len(_ANY_PCT_RE.findall(text))
    suspicious = any_count - valid_count
    if suspicious <= 0:
        return
    print(
        f"[notebook] WARNING: {py} has {suspicious} `# %%` line(s) that don't "
        "look like valid cell markers (no metadata, no trailing whitespace). "
        "These may silently split cells on `jupytext --sync`. Offending lines:",
        file=sys.stderr,
    )
    for ln_no, line in enumerate(text.splitlines(), 1):
        if _ANY_PCT_RE.match(line) and not _VALID_MARKER_RE.match(line):
            print(f"    {py}:{ln_no}: {line[:80]}", file=sys.stderr)


def _surface_stderr(e: subprocess.CalledProcessError, what: str) -> SystemExit:
    """V1-H14: surface jupytext stderr instead of silent CalledProcessError."""
    err = (e.stderr.decode() if isinstance(e.stderr, bytes)
           else (e.stderr or "")).strip()
    return SystemExit(f"[notebook] {what} failed (exit {e.returncode}):\n  {err}")


def sync_from_ipynb(notebook: Path) -> None:
    """Propagate .ipynb → .py via `jupytext --sync`. Best-effort."""
    if not shutil.which("jupytext"):
        raise RuntimeError("jupytext not on PATH")
    py = _paired_py(notebook)
    if not py.exists():
        return
    pre_ids = _ipynb_cell_ids(notebook)
    try:
        subprocess.run(
            ["jupytext", "--sync", str(notebook)],
            check=True, capture_output=True, text=False,
        )
    except subprocess.CalledProcessError as e:
        raise _surface_stderr(e, f"jupytext --sync {notebook}")
    post_ids = _ipynb_cell_ids(notebook)
    _warn_id_loss(pre_ids, post_ids, notebook)


def sync_from_py(notebook: Path) -> None:
    """Propagate .py → .ipynb via `jupytext --sync`, then atomic-rewrite the .ipynb.

    `jupytext --sync` writes .ipynb non-atomically; we re-read and atomic-replace
    to fix that.
    """
    if not shutil.which("jupytext"):
        raise RuntimeError("jupytext not on PATH")
    py = _paired_py(notebook)
    if not py.exists():
        raise RuntimeError(f"no paired .py at {py}")
    _check_pct_literal_in_py(py)
    pre_ids = _ipynb_cell_ids(notebook)
    try:
        subprocess.run(
            ["jupytext", "--sync", str(py)],
            check=True, capture_output=True, text=False,
        )
    except subprocess.CalledProcessError as e:
        raise _surface_stderr(e, f"jupytext --sync {py}")
    nb = nbformat.read(str(notebook), as_version=4)
    atomic_write_ipynb(notebook, nb)
    post_ids = _ipynb_cell_ids(notebook)
    _warn_id_loss(pre_ids, post_ids, notebook)


def _warn_id_loss(pre_ids: set[str], post_ids: set[str], notebook: Path) -> None:
    """V1-H10: emit WARN if jupytext lost IDs during content-mismatch fallback."""
    lost = pre_ids - post_ids
    if lost:
        print(f"[notebook] WARN: jupytext --sync lost {len(lost)} cell ID(s): "
              f"{sorted(lost)[:3]}{'...' if len(lost) > 3 else ''}. "
              f"Plans referencing these IDs in {notebook} will break. "
              "Inspect with `nb diff` and consider `nb regenerate --from-py` if "
              "divergence is suspected.", file=sys.stderr)


def cmd_sync(args) -> int:
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        sys.exit(f"[notebook] not found: {nb_path}")
    py = _paired_py(nb_path)
    with notebook_lock(nb_path, timeout=30.0):
        if py.exists() and py.stat().st_mtime > nb_path.stat().st_mtime:
            sync_from_py(nb_path)
            print(f"[notebook] synced .py → .ipynb: {nb_path}")
        else:
            sync_from_ipynb(nb_path)
            print(f"[notebook] synced .ipynb → .py: {nb_path}")
    return 0


def cmd_regenerate(args) -> int:
    """Drop .ipynb, regenerate from .py via `jupytext --to ipynb`. Loses cell IDs."""
    nb_path: Path = args.notebook.resolve()
    py = _paired_py(nb_path)
    if not py.exists():
        sys.exit(f"[notebook] no paired .py at {py}; nothing to regenerate from")
    if not shutil.which("jupytext"):
        sys.exit("[notebook] jupytext not on PATH")
    print(f"[notebook] WARNING: regenerating {nb_path} from {py}; cell IDs will be reassigned.",
          file=sys.stderr)
    with notebook_lock(nb_path, timeout=30.0):
        try:
            subprocess.run(
                ["jupytext", "--to", "ipynb", str(py), "-o", str(nb_path)],
                check=True, capture_output=True, text=False,
            )
        except subprocess.CalledProcessError as e:
            raise _surface_stderr(e, f"jupytext --to ipynb {py}")
        nb = nbformat.read(str(nb_path), as_version=4)
        atomic_write_ipynb(nb_path, nb)
    print(f"[notebook] regenerated {nb_path}")
    return 0
