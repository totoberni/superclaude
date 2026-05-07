#!/usr/bin/env python3
"""jupytext-regen merge driver.

Registered by `/notebook init` in .git/config:
    git config merge.jupytext-regen.driver "python3 <skill>/templates/merge_regen.py %A %O %B %P"

When git invokes this for a `.ipynb` conflict:
  1. The paired `.py` has been text-merged by git already (independent path).
  2. We discard the conflicting `.ipynb` content and regenerate from the merged `.py`.
  3. Exit 0 unconditionally — there's no .ipynb conflict possible (we regenerate, not merge).

Per W1's empirical recommendation. Avoids nbdime auto-merge silent-cell-deletion footgun (#597).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 5:
        sys.exit("usage: merge_regen.py %A %O %B %P")
    current = Path(sys.argv[1])  # %A — current branch's file (will be overwritten)
    # base = sys.argv[2]          # %O — common ancestor (unused)
    # other = sys.argv[3]         # %B — other branch's file (unused)
    pathname = Path(sys.argv[4])  # %P — path of conflicting file in the working tree

    py = pathname.with_suffix(".py")
    if not py.exists():
        # No paired .py — fall back to nbdime mergetool (manual). Mark as conflict.
        print(f"[notebook merge-regen] no paired .py at {py}; "
              "manual merge required (use `nbdime mergetool {pathname}`)",
              file=sys.stderr)
        return 1
    # UX-4: git's merge-driver subshell does NOT inherit the user's PATH (no
    # .venv/bin), so `shutil.which("jupytext")` typically returns None inside
    # the merge env. Resolve via the absolute path captured at `nb init` time.
    # CWD inside a merge driver is the repo root, so `.notebook/` is reachable.
    jt_path_file = Path.cwd() / ".notebook" / "jupytext_path"
    jupytext: str | None = None
    if jt_path_file.exists():
        candidate = jt_path_file.read_text().strip()
        if candidate and Path(candidate).exists():
            jupytext = candidate
    if jupytext is None:
        jupytext = shutil.which("jupytext")
    if jupytext is None:
        print("[notebook merge-regen] jupytext not resolvable "
              "(missing .notebook/jupytext_path AND not on PATH); "
              "manual merge required. "
              "Re-run `nb init --migrate` to capture jupytext path.",
              file=sys.stderr)
        return 1
    # Regenerate the .ipynb from the (already text-merged) .py.
    subprocess.run(
        [jupytext, "--to", "ipynb", str(py), "-o", str(current)],
        check=True, capture_output=True, text=True,
    )
    print(f"[notebook merge-regen] regenerated {pathname} from {py}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
