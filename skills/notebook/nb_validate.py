r"""/notebook skill — validators.

Runs in order:
1. nbformat.validate — JSON schema
2. AST parse on every code cell — Python syntax
3. pylatexenc on markdown LaTeX (markdown-it-py-tokenized; fence/html/MyST skipped)
4. firewall scan — em-dash + superclaude-meta-leak per ~/.claude/rules/20-tool-conventions.md
5. forbidden-imports scan — config at <dir>/.notebook/forbidden_imports.txt

Exit 0 = pass; non-zero with line/cell pointer on failure.

V1.1 — addresses example-project-review-of-skill-v1.md issues:
  - V1-B2: pylatexenc 2.x + 3.x compat shim, custom macro allowlist for
           `\operatorname`, `\bra`, `\ket`, `\braket`, etc., default tolerant=True
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import nbformat

# Forbidden patterns per ~/.claude/rules/20-tool-conventions.md §Superclaude ↔ Local Codebase Firewall
_FIREWALL_PATTERNS = [
    (r"~/\.claude/", "agent-memory path leak"),
    (r"\bagent-memory\b", "agent-memory path leak"),
    (r"\bMEMORY\.md\b", "agent-memory file leak"),
    (r"\b(?:MM-|GM-|MT-|CW-)\d+\b", "agent-memory ID leak"),
    (r"\bmeta says\b", "meta-structure prose leak"),
    (r"according to the (project memory|gotchas file)", "meta-structure prose leak"),
]
_EM_DASH_RE = re.compile(r"(?<!-)---(?!-)")  # triple dash but not ---- horizontal rules

# V1-B2: macros that pylatexenc's default DB lacks. Allowing them sidesteps
# false-positives on common quantum / linear-algebra notation.
_EXTRA_MACROS_NOARG = ["bra", "ket", "operatorname", "mathbb", "mathcal",
                      "mathrm", "mathbf", "mathfrak", "boldsymbol",
                      "Tr", "tr", "vec", "hat", "tilde", "bar", "dot", "ddot"]
_EXTRA_MACROS_1ARG = ["braket", "ketbra", "norm", "abs", "qty"]


def _build_latex_walker(frag: str):
    """V1-B2 compat shim: works with pylatexenc 2.x and 3.x.

    Returns a configured `LatexWalker` instance, or None if the library is
    missing entirely (caller should skip silently — but loudly, see
    cmd_validate's check at the top).
    """
    try:
        from pylatexenc.latexwalker import LatexWalker
    except ImportError:
        return None
    # Try v3 path first (MacrosSpec + latex_context).
    try:
        from pylatexenc.macrospec import MacroSpec  # v2 + v3
        from pylatexenc import latexwalker
        from pylatexenc.latexwalker import get_default_latex_context_db
        ctx = get_default_latex_context_db()
        try:
            ctx.add_context_category(
                "skill-extra-macros",
                macros=[MacroSpec(m, "") for m in _EXTRA_MACROS_NOARG]
                       + [MacroSpec(m, "{") for m in _EXTRA_MACROS_1ARG],
                prepend=True,
            )
        except (AttributeError, TypeError):
            # v2.x has different add_context_category signature; fall through.
            pass
        return LatexWalker(frag, latex_context=ctx, tolerant_parsing=True)
    except ImportError:
        # Pure v2.x — no latex_context kwarg.
        return LatexWalker(frag, tolerant_parsing=True)


def _validate_schema(nb_path: Path) -> list[str]:
    nb = nbformat.read(str(nb_path), as_version=4)
    try:
        nbformat.validate(nb)
        return []
    except nbformat.ValidationError as e:
        return [f"{nb_path}: nbformat.validate failed: {e}"]


def _validate_ast(nb_path: Path) -> list[str]:
    nb = nbformat.read(str(nb_path), as_version=4)
    errors = []
    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        # Strip Jupyter magics that ast can't parse.
        src = re.sub(r"^\s*[!%]\S.*$", "", src, flags=re.MULTILINE)
        try:
            ast.parse(src)
        except SyntaxError as e:
            errors.append(f"{nb_path}:cell{i}:line{e.lineno}: SyntaxError: {e.msg}")
    return errors


def _validate_md_latex(nb_path: Path) -> list[str]:
    try:
        from pylatexenc.latexwalker import LatexWalkerParseError
        from markdown_it import MarkdownIt
    except ImportError:
        # V1-B2: surface the missing-dep state loudly. Default: warn, not silent.
        return [f"{nb_path}: pylatexenc + markdown-it-py required for LaTeX validation "
                "(install: pip install -r ~/.claude/skills/notebook/templates/requirements-skill.txt)"]
    nb = nbformat.read(str(nb_path), as_version=4)
    md = MarkdownIt()
    math_re = re.compile(r"(?<!\\)\$\$(.+?)\$\$|(?<!\\)\$(.+?)\$", re.DOTALL)
    env_re = re.compile(
        r"\\begin\{(align\*?|equation\*?|gather\*?)\}(.+?)\\end\{\1\}",
        re.DOTALL,
    )
    errors = []
    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "markdown":
            continue
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        for tok in md.parse(src):
            if tok.type in ("fence", "code_block", "html_block"):
                continue
            if tok.type == "inline" and tok.content:
                for m in math_re.finditer(tok.content):
                    frag = m.group(1) or m.group(2)
                    walker = _build_latex_walker(frag)
                    if walker is None:
                        continue
                    try:
                        walker.get_latex_nodes(pos=0)
                    except LatexWalkerParseError as e:
                        errors.append(f"{nb_path}:cell{i}: LaTeX parse error: {e} | {frag[:80]!r}")
                for m in env_re.finditer(tok.content):
                    walker = _build_latex_walker(m.group(0))
                    if walker is None:
                        continue
                    try:
                        walker.get_latex_nodes(pos=0)
                    except LatexWalkerParseError as e:
                        errors.append(f"{nb_path}:cell{i}: LaTeX env parse error: {e}")
    return errors


def _firewall_scan(nb_path: Path) -> list[str]:
    nb = nbformat.read(str(nb_path), as_version=4)
    errors = []
    for i, cell in enumerate(nb.cells):
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        for pat, label in _FIREWALL_PATTERNS:
            for m in re.finditer(pat, src):
                errors.append(f"{nb_path}:cell{i}: firewall: {label} ({m.group(0)!r})")
        if cell.cell_type == "markdown":
            for m in _EM_DASH_RE.finditer(src):
                errors.append(f"{nb_path}:cell{i}: triple-dash em-dash detected (use en-dash or rewrite)")
    return errors


def _forbidden_imports(nb_path: Path) -> list[str]:
    cfg = nb_path.parent / ".notebook" / "forbidden_imports.txt"
    if not cfg.exists():
        return []
    forbidden = [ln.strip() for ln in cfg.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")]
    if not forbidden:
        return []
    nb = nbformat.read(str(nb_path), as_version=4)
    errors = []
    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue
        src = cell.source if isinstance(cell.source, str) else "".join(cell.source)
        for forbidden_pat in forbidden:
            if re.search(forbidden_pat, src):
                errors.append(f"{nb_path}:cell{i}: forbidden import: {forbidden_pat!r}")
    return errors


def cmd_validate(args) -> int:
    nb_path: Path = args.notebook.resolve()
    if not nb_path.exists():
        sys.exit(f"[notebook] not found: {nb_path}")
    errors: list[str] = []
    errors += _validate_schema(nb_path)
    if not errors:
        errors += _validate_ast(nb_path)
        errors += _validate_md_latex(nb_path)
        errors += _firewall_scan(nb_path)
        errors += _forbidden_imports(nb_path)
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"[notebook] validate ok: {nb_path}")
    return 0
