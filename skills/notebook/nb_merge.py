"""/notebook skill — merge-preview subcommand (UX-3).

Pre-simulate cross-branch `.ipynb` merges by extracting both branches' source
via `git show`, converting to py:percent via jupytext, then running
`git merge-file` against tempfiles outside the working tree. Surfaces conflict
topology (count, line ranges, surrounding `# %% [tag-id]` markers) so orchs
can plan resolution before invoking the real `git merge` (which engages the
`jupytext-regen` driver and locks the index).

V1.3 — addresses example-project-review-of-skill-v3.md UX-3.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from nb_edit import SkillError
from nb_io import canonical_repo_root


class MergePreviewError(SkillError):
    """`nb merge-preview` could not extract sources or run merge-file."""


# Detects jupytext py:percent cell separators: `# %% [tag-or-id]`.
_PCT_HEADER_RE = re.compile(r"^# %%\s*(?:\[(?P<id>[^\]]*)\])?", re.MULTILINE)


def _resolve_jupytext(repo_root: Path) -> str:
    """V3-X4 / UX-4 fallback chain: prefer `.notebook/jupytext_path` (captured
    at init time by `_capture_jupytext_path`), else `shutil.which`. Raises
    `MergePreviewError` if neither resolves to an existing file.
    """
    jt_path_file = repo_root / ".notebook" / "jupytext_path"
    if jt_path_file.exists():
        candidate = jt_path_file.read_text().strip()
        if candidate and Path(candidate).exists():
            return candidate
    found = shutil.which("jupytext")
    if not found:
        raise MergePreviewError(
            "jupytext not found on PATH and `.notebook/jupytext_path` is empty. "
            "Run `nb init --migrate` to register the project venv jupytext."
        )
    return found


def _git_show(root: Path, branch: str, rel: str, dest: Path) -> None:
    """Extract <branch>:<rel> into `dest` (binary-safe)."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "show", f"{branch}:{rel}"],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        raise MergePreviewError(
            f"git show {branch}:{rel} failed (branch missing or path not in tree?): "
            f"{e.stderr.decode('utf-8', 'replace').strip()}"
        ) from e
    dest.write_bytes(out)


def _to_percent(jupytext_bin: str, ipynb: Path, dest: Path) -> None:
    """Convert .ipynb → py:percent at `dest`."""
    try:
        subprocess.check_call(
            [jupytext_bin, "--to", "py:percent", str(ipynb), "-o", str(dest)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        raise MergePreviewError(
            f"jupytext --to py:percent {ipynb} failed: "
            f"{(e.stderr or b'').decode('utf-8', 'replace').strip()}"
        ) from e


def _merge_base(root: Path, ours: str, theirs: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "merge-base", ours, theirs],
            text=True, stderr=subprocess.PIPE,
        ).strip()
    except subprocess.CalledProcessError as e:
        raise MergePreviewError(
            f"git merge-base {ours} {theirs} failed: "
            f"{e.stderr.strip()}"
        ) from e


def _summarise_conflicts(merged_py: Path) -> tuple[int, list[dict]]:
    """Walk the merged py:percent file. For each `<<<<<<<` block, capture the
    line range and the nearest preceding `# %% [id]` header (used as the
    topology landmark — orchs know which cell the conflict belongs to).
    """
    text = merged_py.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    conflicts: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<< "):
            start = i + 1  # 1-indexed for display
            depth_end: int | None = None
            for j in range(i, len(lines)):
                if lines[j].startswith(">>>>>>> "):
                    depth_end = j + 1
                    break
            if depth_end is None:
                conflicts.append({"start": start, "end": len(lines),
                                  "anchor": "<unterminated>"})
                break
            anchor = "<no-cell-marker>"
            for j in range(i - 1, -1, -1):
                m = _PCT_HEADER_RE.match(lines[j])
                if m:
                    anchor = m.group("id") or "<untagged-cell>"
                    break
            conflicts.append({
                "start": start, "end": depth_end, "anchor": anchor,
                "size": depth_end - start + 1,
            })
            i = depth_end
        else:
            i += 1
    return len(conflicts), conflicts


def cmd_merge_preview(args) -> int:
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        raise MergePreviewError(f"notebook not found: {nb_path}")
    root = canonical_repo_root(nb_path.parent)
    try:
        rel = str(nb_path.relative_to(root))
    except ValueError as e:
        raise MergePreviewError(
            f"notebook {nb_path} is outside the canonical repo root {root}"
        ) from e

    ours = args.ours_branch
    theirs = args.theirs_branch
    base = args.base_branch or _merge_base(root, ours, theirs)
    jupytext_bin = _resolve_jupytext(root)

    tmpdir = Path(tempfile.mkdtemp(prefix="nb-merge-preview-"))
    # 1. Extract three .ipynb sides.
    ours_ipynb = tmpdir / "ours.ipynb"
    base_ipynb = tmpdir / "base.ipynb"
    theirs_ipynb = tmpdir / "theirs.ipynb"
    _git_show(root, ours, rel, ours_ipynb)
    _git_show(root, base, rel, base_ipynb)
    _git_show(root, theirs, rel, theirs_ipynb)

    # 2. Convert each to py:percent.
    ours_py = tmpdir / "ours.py"
    base_py = tmpdir / "base.py"
    theirs_py = tmpdir / "theirs.py"
    _to_percent(jupytext_bin, ours_ipynb, ours_py)
    _to_percent(jupytext_bin, base_ipynb, base_py)
    _to_percent(jupytext_bin, theirs_ipynb, theirs_py)

    # 3. git merge-file writes in place into ours_py; exit code = #conflicts
    # (or 127 on error). We capture for reporting.
    cmd = ["git", "merge-file"]
    if args.diff3:
        cmd.append("--diff3")
    cmd += [str(ours_py), str(base_py), str(theirs_py)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode < 0 or res.returncode == 127:
        raise MergePreviewError(
            f"git merge-file failed (exit={res.returncode}): {res.stderr.strip()}"
        )

    n, blocks = _summarise_conflicts(ours_py)
    print(f"[notebook] merge-preview: {ours} <- {theirs} (base: {base[:8]})")
    print(f"[notebook] conflicts: {n}")
    for k, b in enumerate(blocks):
        print(f"  [{k}] lines {b['start']}-{b['end']} "
              f"({b.get('size', '?')} ln) anchor={b['anchor']}")
    if n == 0:
        print("[notebook] no conflicts — `git merge` would auto-resolve.")
        # On clean preview, clean up the tempdir — orch doesn't need to inspect.
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 0
    # On conflicts, retain tempdir so orch can `head` / inspect markers.
    print(f"[notebook] preview file: {ours_py} (kept for inspection)")
    print(f"[notebook] tempdir: {tmpdir} (delete manually when done)")
    return 1
