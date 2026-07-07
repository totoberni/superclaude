"""Vendor-independence seal (W5.1 spec section 7 / Stage-7 seal criterion iv).

A STANDING structural invariant: no vendor plugin package may import a SIBLING
vendor plugin package. Each `engine/providers/<vendor>/` subtree must depend
only on the kernel, its own package, the shared provider base/registry/protocol
modules, the transitional `engine.*` shims, stdlib, and third-party code --
NEVER on `engine.providers.<other-vendor>`. This locks the disjointness that
commit d226065 achieved (the three non-greenhouse plugins stopped delegating to
`greenhouse.resolve_values`; all four now delegate to
`engine.kernel.resolve.resolve_values`).

The check AST-walks EVERY `.py` under each vendor subtree and inspects EVERY
`import` / `from ... import ...` node ANYWHERE in the tree -- including
function-local (lazy) imports -- resolving relative imports to their absolute
dotted names against the importing file's own package. It bites ONLY on a
cross-vendor import; kernel, self, shared-provider, shim, stdlib, and
third-party imports are all left untouched by construction (the assertion only
matches `engine.providers.<sibling>`). Docstring/comment mentions of a sibling
package are never flagged because only real AST import nodes are inspected.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

import engine.providers

# The four vendor plugin packages under test. Disjointness is asserted pairwise:
# each vendor may not import any of the OTHER three.
VENDORS = frozenset({"greenhouse", "lever", "ashby", "workable"})

# Anchor the walk to the real, imported `engine.providers` package so the test
# never hard-codes a path outside the repo.
_PROVIDERS_DIR = Path(engine.providers.__file__).resolve().parent


def _package_of(pyfile: Path) -> str:
    """The absolute dotted package name a file's relative imports resolve against.

    For both `engine/providers/<vendor>/foo.py` and the package's own
    `__init__.py`, this is the containing directory's dotted name
    (`engine.providers.<vendor>[.<subpkg>...]`) -- the anchor `importlib` uses
    to turn a `from . import x` / `from ..base import y` into an absolute name.
    """
    rel = pyfile.relative_to(_PROVIDERS_DIR)
    return "engine.providers" + "".join(f".{part}" for part in rel.parent.parts)


def _imported_module_names(pyfile: Path) -> list[tuple[str, int]]:
    """Every absolute module name imported anywhere in `pyfile`, with line numbers.

    Covers module-level AND function-local/lazy imports (via `ast.walk`). For a
    `from X import y` node, both the base module `X` and the candidate submodule
    `X.y` are emitted, so `from engine.providers import greenhouse` (submodule
    import) is caught by the `engine.providers.greenhouse` entry, and
    `from engine.providers.greenhouse import resolve_values` is caught by its
    base. Relative imports are resolved to absolute against the file's package.
    """
    package = _package_of(pyfile)
    tree = ast.parse(pyfile.read_text(), filename=str(pyfile))
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = importlib.util.resolve_name(
                    "." * node.level + (node.module or ""), package)
            else:
                base = node.module or ""
            if base:
                names.append((base, node.lineno))
            for alias in node.names:
                if alias.name == "*":
                    continue
                combined = f"{base}.{alias.name}" if base else alias.name
                names.append((combined, node.lineno))
    return names


def _sibling_hit(module_name: str, siblings: frozenset[str]) -> str | None:
    """The offending sibling-vendor package `module_name` names, or None.

    Matches an exact sibling package (`engine.providers.lever`) or any submodule
    under it (`engine.providers.lever.fill`), but NOT the plugin's own package.
    """
    for sibling in siblings:
        pkg = f"engine.providers.{sibling}"
        if module_name == pkg or module_name.startswith(pkg + "."):
            return pkg
    return None


@pytest.mark.parametrize("vendor", sorted(VENDORS))
def test_vendor_plugin_does_not_import_a_sibling_vendor(vendor: str) -> None:
    """No `engine/providers/<vendor>/` file may import a sibling vendor package."""
    vendor_dir = _PROVIDERS_DIR / vendor
    assert vendor_dir.is_dir(), f"vendor package dir not found: {vendor_dir}"

    siblings = VENDORS - {vendor}
    offenders: list[str] = []
    scanned = 0
    for pyfile in sorted(vendor_dir.rglob("*.py")):
        scanned += 1
        for module_name, lineno in _imported_module_names(pyfile):
            hit = _sibling_hit(module_name, siblings)
            if hit is not None:
                rel = pyfile.relative_to(_PROVIDERS_DIR.parent.parent)
                offenders.append(
                    f"{rel}:{lineno} imports {module_name!r} "
                    f"(sibling vendor package {hit!r})")

    # Guard against a silent no-op: the vendor subtree must have real .py files.
    assert scanned > 0, f"no .py files scanned under {vendor_dir}"
    assert not offenders, (
        f"vendor-independence seal violated: {vendor!r} imports a sibling "
        f"vendor package (spec section 7 / Stage-7 seal criterion iv). "
        f"Each plugin must delegate to engine.kernel.*, never to another "
        f"vendor plugin. Offending import(s):\n  " + "\n  ".join(offenders))
