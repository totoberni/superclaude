"""/notebook skill — read-only audits.

Subsumes the firewall_scan / dash_scan / lib_audit forks observed in
example-project and example-cw (Cluster H-4). Centralised here.

All checks are read-only — never mutate the notebook.

V1.1 — addresses example-project-review-of-skill-v1.md issues:
  - V1-B1: savefig regex configurable + broader default (catches helper wrappers)
  - V1-H15: --warn flag (soft warning vs hard fail) for migration friction

V1.3 — addresses example-project-review-of-skill-v3.md V3-X2:
  - regex now actually matches helper form `savefig(fig, "name")` (V1-B1
    shipped only the prefix-half; arg-shape was still single-quoted-arg)
  - comment ↔ savefig comparison canonicalises to `Path(...).stem` so
    `# fig: report/img/foo.png` ↔ helper `savefig(fig, "foo")` matches
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import nbformat

from nb_validate import _firewall_scan, _forbidden_imports

_FIG_COMMENT_RE = re.compile(r"^\s*#\s*fig:\s*(\S+)\s*$", re.MULTILINE)
# V1-B1 + V3-X2: catches BOTH matplotlib `savefig("name")` AND helper
# `savefig(fig, "name")`. Two alternatives → 2-tuple per match; consumers
# collapse via `next((g for g in m if g), "")`. Per-project overrides via
# `<nb-dir>/.notebook/savefig_pattern.txt` (must capture path in ANY group).
_DEFAULT_SAVEFIG_RE = re.compile(
    r"\b(?:plt\.)?savefig\s*\(\s*"
    r"(?:['\"]([^'\"]+)['\"]"           # matplotlib form: savefig("name")
    r"|\w+\s*,\s*['\"]([^'\"]+)['\"])"  # helper form: savefig(fig, "name")
)
_DEFAULT_DYNAMIC_SAVEFIG_RE = re.compile(r"\b(?:plt\.)?savefig\s*\(\s*f['\"]")


def _load_savefig_patterns(nb_path: Path) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Per-project savefig regex via `<dir>/.notebook/savefig_pattern.txt`.

    File format: one regex per line, comments with `#`. The regex MUST capture
    the path string (group 1). Empty/missing file → defaults.
    Dynamic-detection (f-strings) is hard-coded since loop-savefig is forbidden.
    """
    cfg = nb_path.parent / ".notebook" / "savefig_pattern.txt"
    if not cfg.exists():
        return _DEFAULT_SAVEFIG_RE, _DEFAULT_DYNAMIC_SAVEFIG_RE
    pats = []
    for ln in cfg.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        pats.append(ln)
    if not pats:
        return _DEFAULT_SAVEFIG_RE, _DEFAULT_DYNAMIC_SAVEFIG_RE
    combined = "|".join(f"(?:{p})" for p in pats)
    try:
        return re.compile(combined), _DEFAULT_DYNAMIC_SAVEFIG_RE
    except re.error as e:
        print(f"[notebook] savefig_pattern.txt regex compile failed: {e}; using default",
              file=sys.stderr)
        return _DEFAULT_SAVEFIG_RE, _DEFAULT_DYNAMIC_SAVEFIG_RE


def _check_em_dash(nb_path: Path) -> list[str]:
    nb = nbformat.read(str(nb_path), as_version=4)
    errs = []
    em_re = re.compile(r"(?<!-)---(?!-)")
    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "markdown":
            continue
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        for m in em_re.finditer(src):
            errs.append(f"{nb_path}:cell{i}:offset{m.start()}: triple-dash em-dash; "
                        "use en-dash (U+2013) or rewrite")
    return errs


def _check_fig_binding(nb_path: Path) -> list[str]:
    """`# fig: <path>` ↔ savefig symmetry (any helper-wrapper, configurable)."""
    nb = nbformat.read(str(nb_path), as_version=4)
    save_re, dyn_re = _load_savefig_patterns(nb_path)
    errs = []
    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        comments = _FIG_COMMENT_RE.findall(src)
        # V3-X2: regex may yield tuples (multi-group alternation) or strings
        # (single-group user override). Collapse to a list[str] of names.
        raw = save_re.findall(src)
        savefigs = [
            (next((g for g in m if g), "") if isinstance(m, tuple) else m)
            for m in raw
        ]
        if dyn_re.search(src):
            errs.append(f"{nb_path}:cell{i}: dynamic savefig (f-string path); "
                        "split into one cell per figure")
            continue
        if not comments and not savefigs:
            continue  # no savefig in this cell, no comment expected
        if len(comments) != len(savefigs):
            errs.append(
                f"{nb_path}:cell{i}: # fig: comments={len(comments)} but "
                f"savefig calls={len(savefigs)} — must be equal"
            )
            continue
        # V3-X2: canonicalise both ends to filename stem so
        # `# fig: report/img/foo.png` matches helper `savefig(fig, "foo")`.
        for c, s in zip(comments, savefigs):
            if Path(c).stem != Path(s).stem:
                errs.append(f"{nb_path}:cell{i}: # fig: {c!r} != savefig {s!r}")
    return errs


_CHECKS = {
    "em-dash": _check_em_dash,
    "firewall": _firewall_scan,
    "forbidden-imports": _forbidden_imports,
    "fig-binding": _check_fig_binding,
}


def cmd_audit(args) -> int:
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        sys.exit(f"[notebook] not found: {nb_path}")
    checks = args.check or list(_CHECKS.keys())
    unknown = [c for c in checks if c not in _CHECKS]
    if unknown:
        sys.exit(f"[notebook] unknown checks: {unknown}; available: {list(_CHECKS.keys())}")
    all_errs = []
    for c in checks:
        errs = _CHECKS[c](nb_path)
        for e in errs:
            level = "WARN" if args.warn else "ERROR"
            print(f"[{c}] {level}: {e}", file=sys.stderr)
        all_errs += errs
    if all_errs and not args.warn:
        return 1
    if all_errs and args.warn:
        print(f"[notebook] audit completed with {len(all_errs)} warning(s) "
              f"(--warn): {nb_path}", file=sys.stderr)
        return 0
    print(f"[notebook] audit ok: {nb_path} ({', '.join(checks)})")
    return 0
