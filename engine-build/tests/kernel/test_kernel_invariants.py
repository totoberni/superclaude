"""Structural invariant tests for the W5.1 kernel (``engine/kernel/``).

These pin the kernel's SAFETY and LAYERING properties so they are enforced by
the suite forever, not by one-off session checks. Six invariants:

1. Guard seal      - ``kernel/never_send.py`` stays byte-identical (per-function
                     source SHA-256) to the sealed baseline at git tag
                     ``never-send-sealed-v1`` (the guard's pre-move home,
                     ``engine-build/engine/providers/base.py``). The never-send
                     guard is FROZEN.
2. Layering        - no kernel module imports anything ABOVE the ssot line
                     (``engine.providers``/``run``/``fill``/``fieldmap``/... are
                     forbidden anywhere in the tree), save two documented
                     non-load-time seams (see ``_KNOWN_UPWARD_EXCEPTIONS``).
3. Load-time clean - the STRICT form of (2): zero forbidden imports execute at
                     kernel import time (proves the two allowlisted seams are the
                     only exceptions and are both deferred, never load-bearing).
4. Browser-free    - importing the kernel pulls in NEITHER patchright NOR
                     playwright (the daily poller must never load the browser
                     stack).
5. Shim identity   - the pre-move import paths still resolve to the SAME objects
                     as the kernel paths (the re-export shims are pass-through).
6. Posting seam    - ``Posting.vendor_extra`` defaults to a per-instance empty
                     dict that accepts arbitrary keys (the kernel-stays-vendor-
                     agnostic overflow seam).

Fully repo-local by construction: every path is resolved from this file's own
location (walking up to the ``.git`` root) and from the in-repo ``engine``
package; nothing outside the repository is referenced.
"""

from __future__ import annotations

import ast
import hashlib
import os
import shutil
import ssl  # noqa: F401  (see below)
import subprocess
import sys
from pathlib import Path

import pytest

from engine.providers.lever.capture import capture_lever

# Pre-warm `ssl` at collection time. The suite's autouse `no_network` fixture
# (tests/conftest.py) replaces `socket.socket` with a plain function for the
# duration of every test. If `ssl` is imported for the FIRST time DURING a test
# (as the shim-identity test would, via engine.fetch), ssl.py's
# `class SSLSocket(socket)` subclasses that function and dies with a spurious
# `TypeError`. Importing `ssl` here -- during collection, before any per-test
# fixture runs and while `socket.socket` is still the real class -- caches the
# real `SSLSocket`, so every later `import ssl` is a harmless no-op. In the full
# suite `ssl` is already cached by other modules; this only matters when
# tests/kernel/ runs in isolation.


# --------------------------------------------------------------------------- #
# Repo-local path resolution (no reference to anything outside the repository).
# --------------------------------------------------------------------------- #

def _repo_root() -> Path:
    """The git working-tree root, found by walking up from this test file.

    ``.git`` is a directory in a normal clone and a file in a linked worktree /
    submodule, so ``.exists()`` covers both. Falls back to the documented layout
    ``engine-build/tests/kernel/<this file>`` -> ``parents[3]`` when no ``.git``
    marker is found (e.g. an exported tree); the seal test then fails LOUDLY on
    the ``git show`` rather than silently mis-resolving.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return here.parents[3]


# ``engine-build`` (the dir that holds the ``engine`` package + ``tests/``).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_KERNEL_DIR = _PROJECT_ROOT / "engine" / "kernel"

# The sealed baseline: the guard's pre-move home, addressed inside the tag.
_SEAL_TAG = "never-send-sealed-v1"
_SEALED_REL_PATH = "engine-build/engine/providers/base.py"

_GIT = shutil.which("git")


# --------------------------------------------------------------------------- #
# Test 1 - guard seal (the safety-critical one).
# --------------------------------------------------------------------------- #

# The 13 frozen symbols that make up the never-send guard. Order is irrelevant
# (compared as a set of per-symbol hashes) but kept verbatim from the seal spec.
_GUARD_SYMBOLS = (
    "_is_submit_request",
    "_graphql_submit_match",
    "_graphql_operation_names",
    "_url_op_params",
    "_all_ops_carry_inline_query",
    "_never_send_handler",
    "_request_post_data",
    "_route_target",
    "install_never_send",
    "_SUBMIT_URL_PATTERNS",
    "_SUBMIT_GRAPHQL_URL_PATTERNS",
    "_SUBMIT_OPERATION_RE",
    "_GRAPHQL_MUTATION_RE",
)


def _guard_symbol_hashes(source: str) -> dict[str, str]:
    """Map each guard symbol to the SHA-256 of its top-level source segment.

    Only top-level ``FunctionDef`` and ``Assign`` statements are considered (the
    guard is exactly these two shapes); ``ast.get_source_segment`` returns the
    exact ``def ...``/``NAME = ...`` slice, so the hash is over the guard's own
    source bytes, independent of surrounding module text.
    """
    tree = ast.parse(source)
    hashes: dict[str, str] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.FunctionDef):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for name in names:
            if name in _GUARD_SYMBOLS:
                segment = ast.get_source_segment(source, node)
                assert segment is not None, f"no source segment for {name}"
                hashes[name] = hashlib.sha256(segment.encode("utf-8")).hexdigest()
    return hashes


@pytest.mark.skipif(
    _GIT is None,
    reason="git binary unavailable; the guard-seal test needs git to read the "
           "sealed baseline from tag " + _SEAL_TAG,
)
def test_never_send_guard_seal_per_function_hashes():
    """Every frozen guard symbol is byte-identical (per-function source hash) to
    the sealed baseline at tag ``never-send-sealed-v1``.

    A MISSING TAG is a hard FAILURE, never a skip: the seal is load-bearing (a
    silently-absent baseline would let the guard drift undetected). Only a
    genuinely absent ``git`` binary skips this test.
    """
    current_src = (_KERNEL_DIR / "never_send.py").read_text()

    try:
        sealed_src = subprocess.check_output(
            [_GIT, "show", f"{_SEAL_TAG}:{_SEALED_REL_PATH}"],
            cwd=_repo_root(),
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"cannot read sealed baseline '{_SEAL_TAG}:{_SEALED_REL_PATH}': "
            f"{(exc.stderr or '').strip()}. The never-send guard SEAL IS "
            "LOAD-BEARING -- a missing tag/path is a hard failure, not a skip. "
            "Restore the tag or escalate as a kernel review; do not delete it "
            "to make this test pass."
        )

    current = _guard_symbol_hashes(current_src)
    sealed = _guard_symbol_hashes(sealed_src)

    missing_current = [s for s in _GUARD_SYMBOLS if s not in current]
    missing_sealed = [s for s in _GUARD_SYMBOLS if s not in sealed]
    assert not missing_current, (
        f"guard symbol(s) absent from current kernel/never_send.py: "
        f"{missing_current}"
    )
    assert not missing_sealed, (
        f"guard symbol(s) absent from sealed baseline {_SEAL_TAG}: "
        f"{missing_sealed}"
    )

    diverging = sorted(s for s in _GUARD_SYMBOLS if current[s] != sealed[s])
    assert not diverging, (
        f"never-send guard symbol(s) {diverging} DIVERGE from the sealed tag "
        f"'{_SEAL_TAG}'. The never-send guard is FROZEN: any change is a "
        "reviewed KERNEL escalation (two-round hostile review + empirical "
        "allow-set diff), NEVER a plugin edit. If this change is intentional, "
        "re-seal the tag through that review; otherwise revert to the sealed "
        "source."
    )


# --------------------------------------------------------------------------- #
# Tests 2 & 3 - kernel layering (imports nothing above ssot).
# --------------------------------------------------------------------------- #

# Prefixes a kernel module must NOT import (anything ABOVE the ssot line). A
# match is an exact module name OR a dotted child (``p`` or ``p.<child>``).
_FORBIDDEN_PREFIXES = (
    "engine.providers",
    "engine.run",
    "engine.fetch",
    "engine.match",
    "engine.draft",
    "engine.fill",
    "engine.fieldmap",
    "engine.notify",
    "engine.store",
    "engine.queue_sm",
    "engine.questionnaire",
    "engine.artifacts",
    "engine.profile_map",
    "engine.ingest",
    "engine.validate",
    "engine.config",
)

# Documented, intentional NON-LOAD-TIME upward references, keyed by
# (kernel_file_basename, imported_module), proven load-time-clean by
# ``test_kernel_no_loadtime_upward_imports``:
#   * contracts.py -- a TRANSITIONAL CALL-TIME import inside
#                     ``FieldMap.coverage()``; the deliberate anti-cycle seam (an
#                     eager import would recreate the fetch -> providers ->
#                     fieldmap -> fetch cycle). The kernel module still imports
#                     standalone. The classifier now lives in
#                     ``engine.kernel.resolve``, but the method must keep routing
#                     through the ``engine.fieldmap.coverage`` shim because that
#                     shim default-injects the Greenhouse widget resolver for the
#                     live method callers (run.py:301,621). Dies in W5.1 Stage 3
#                     when callers inject via the registry -- remove the entry
#                     then.
# (protocol.py's TYPE_CHECKING FieldMap import was repointed to
# ``engine.kernel.contracts`` during Stage-0 review remediation, so it no longer
# needs an entry here.)
# This allowlist is SUBSET-checked: entries may be REMOVED (a refactor that drops
# a seam is an improvement and must not break the suite) but adding a new entry
# requires the same kernel-escalation review as a guard change -- a new forbidden
# import is a layering regression, not a convenience.
_KNOWN_UPWARD_EXCEPTIONS = frozenset({
    ("contracts.py", "engine.fieldmap"),
})


def _kernel_module_files() -> list[Path]:
    files = sorted(p for p in _KERNEL_DIR.glob("*.py"))
    assert files, f"no kernel module files found under {_KERNEL_DIR}"
    return files


def _is_forbidden(module: str | None) -> bool:
    if not module:
        return False
    return any(module == p or module.startswith(p + ".")
               for p in _FORBIDDEN_PREFIXES)


def _all_imported_modules(tree: ast.AST) -> list[str]:
    """Every imported module name ANYWHERE in the tree (top-level, function-local
    lazy imports, class bodies, ``TYPE_CHECKING`` blocks -- ``ast.walk`` reaches
    all of them)."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (node.level > 0): they resolve within the
            # kernel package itself, never above it.
            if node.module and node.level == 0:
                modules.append(node.module)
    return modules


def test_kernel_layering_no_forbidden_imports():
    """No kernel module imports anything above the ssot line, ANYWHERE in the
    file (including function-local lazy imports), except the two documented
    non-load-time seams in ``_KNOWN_UPWARD_EXCEPTIONS``."""
    violations: set[tuple[str, str]] = set()
    for path in _kernel_module_files():
        tree = ast.parse(path.read_text())
        for module in _all_imported_modules(tree):
            if _is_forbidden(module):
                violations.add((path.name, module))

    unexpected = violations - _KNOWN_UPWARD_EXCEPTIONS
    assert not unexpected, (
        f"kernel module(s) import ABOVE the ssot line: {sorted(unexpected)}. "
        "The kernel is the vendor-agnostic base: it may import only stdlib, "
        "third-party, and engine.kernel.* . A new upward import is a LAYERING "
        "REGRESSION -- fix the import (push the dependency down into the caller "
        "or use a call-time seam), do NOT add it to the allowlist without a "
        "kernel-escalation review."
    )


class _LoadTimeImportCollector(ast.NodeVisitor):
    """Collect only imports that EXECUTE at module import time.

    Excludes imports inside any function/method body (call-time) and inside any
    ``if TYPE_CHECKING:`` block (typing-only, never executed). A class-body
    import counts as load-time (it runs at class definition), which is the
    conservative, correct classification.
    """

    def __init__(self) -> None:
        self._func_depth = 0
        self._typing_depth = 0
        self.load_time_modules: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_test(node.test):
            self._typing_depth += 1
            self.generic_visit(node)
            self._typing_depth -= 1
        else:
            self.generic_visit(node)

    @staticmethod
    def _is_type_checking_test(test: ast.expr) -> bool:
        # Matches `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:`.
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    def _record(self, module: str | None, level: int) -> None:
        if module and level == 0 and self._func_depth == 0 and self._typing_depth == 0:
            self.load_time_modules.append(module)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, 0)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._record(node.module, node.level)


def test_kernel_no_loadtime_upward_imports():
    """The STRICT layering invariant: ZERO forbidden imports execute at kernel
    import time. This is the property that makes the kernel independently
    importable and browser-free (Test 3); it also proves the two allowlisted
    seams in ``test_kernel_layering_no_forbidden_imports`` are BOTH deferred
    (TYPE_CHECKING / call-time), never load-bearing."""
    load_time_violations: set[tuple[str, str]] = set()
    for path in _kernel_module_files():
        collector = _LoadTimeImportCollector()
        collector.visit(ast.parse(path.read_text()))
        for module in collector.load_time_modules:
            if _is_forbidden(module):
                load_time_violations.add((path.name, module))

    assert not load_time_violations, (
        f"kernel module(s) execute a forbidden upward import AT IMPORT TIME: "
        f"{sorted(load_time_violations)}. A load-time upward import breaks the "
        "kernel's independent-import + browser-free guarantees. Defer it "
        "(call-time inside the method that needs it, or under TYPE_CHECKING if "
        "it is a pure annotation) or push the dependency into the caller."
    )


def test_kernel_import_is_browser_free():
    """Importing the kernel modules pulls in NEITHER patchright NOR playwright.

    Run in a FRESH subprocess so the parent pytest process (which has imported
    the browser-adjacent modules for other tests) cannot contaminate the check.
    """
    probe = (
        "import sys\n"
        "import engine.kernel.contracts\n"
        "import engine.kernel.never_send\n"
        "import engine.kernel.fill_toolkit\n"
        "import engine.kernel.capture_toolkit\n"
        "import engine.kernel.ssot\n"
        "import engine.kernel.protocol\n"
        "import engine.kernel.discover_base\n"
        "assert 'patchright' not in sys.modules, 'patchright leaked into kernel import'\n"
        "assert 'playwright' not in sys.modules, 'playwright leaked into kernel import'\n"
        "print('BROWSER_FREE_OK')\n"
    )
    env = dict(os.environ)
    # Make `engine` importable in the fresh interpreter regardless of its CWD
    # (mirrors pytest.ini's `pythonpath = .`), repo-locally.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_PROJECT_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "browser-free kernel-import probe FAILED "
        f"(rc={result.returncode}).\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "BROWSER_FREE_OK" in result.stdout, (
        f"probe did not confirm browser-free import.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


# --------------------------------------------------------------------------- #
# Test 4 - shim identity (old import paths re-export the SAME kernel objects).
# --------------------------------------------------------------------------- #

def test_shim_paths_are_identity_reexports():
    """Every pre-move import path resolves to the SAME object as its kernel path,
    so existing importers (tests, providers, run.py) keep resolving unchanged."""
    import engine.ssot
    import engine.kernel.ssot
    import engine.providers.protocol
    import engine.kernel.protocol
    import engine.fieldmap
    import engine.kernel.contracts
    import engine.kernel.fill_toolkit
    import engine.providers.base
    import engine.kernel.never_send
    import engine.kernel.capture_toolkit
    import engine.fetch

    assert engine.ssot.SSOT is engine.kernel.ssot.SSOT
    assert engine.providers.protocol.Provider is engine.kernel.protocol.Provider
    assert engine.fieldmap.FieldMap is engine.kernel.contracts.FieldMap
    assert (engine.providers.base.install_never_send
            is engine.kernel.never_send.install_never_send)
    assert engine.providers.base.type_human is engine.kernel.fill_toolkit.type_human
    # UA is a str constant, not a class/function: the re-export contract is VALUE
    # equality (a string's identity across a `from ... import` is a CPython
    # interning implementation detail, not part of the contract), so `==`.
    assert engine.fetch.UA == engine.kernel.capture_toolkit.UA


# --------------------------------------------------------------------------- #
# Test 5 - Posting.vendor_extra seam (per-instance dict, arbitrary keys).
# --------------------------------------------------------------------------- #

def _make_posting(**overrides):
    """A minimal ``Posting`` with dummies for every required (no-default) arg."""
    from engine.kernel.contracts import Posting
    kwargs = dict(
        vendor="greenhouse",
        company_slug="acme",
        job_id="42",
        title="Engineer",
        locations=["Remote"],
        remote_flag=True,
        comp=None,
        posted_ts=None,
        updated_ts=None,
        url="https://example.test/jobs/42",
    )
    kwargs.update(overrides)
    return Posting(**kwargs)


def test_posting_vendor_extra_seam():
    """``Posting.vendor_extra`` defaults to a per-instance empty dict that accepts
    arbitrary keys (the kernel-stays-vendor-agnostic overflow seam)."""
    first = _make_posting()
    second = _make_posting()

    # Default is an empty dict.
    assert first.vendor_extra == {}
    assert second.vendor_extra == {}

    # Per-instance: mutating one instance's dict does not affect another (proves
    # it is a `field(default_factory=dict)`, not a shared class-level `= {}`).
    first.vendor_extra["greenhouse_department_id"] = "eng-7"
    assert second.vendor_extra == {}
    assert first.vendor_extra is not second.vendor_extra

    # Accepts arbitrary keys of arbitrary shape (kernel imposes no schema).
    second.vendor_extra["ashby_form_secondary_locations"] = ["NYC", "SF"]
    second.vendor_extra["lever_workplace_type"] = 3
    assert second.vendor_extra == {
        "ashby_form_secondary_locations": ["NYC", "SF"],
        "lever_workplace_type": 3,
    }


def test_invoking_default_factory_without_patchright_raises_clear_error():
    """A kernel ``capture_toolkit`` invariant: the default browser factory fails
    with a clear, actionable install error when patchright is absent (never a
    bare ImportError). Invoked via ``capture_lever`` with no browser_factory,
    which routes through the kernel default factory."""
    try:
        import patchright  # noqa: F401
    except ImportError:
        patchright_present = False
    else:
        patchright_present = True

    if patchright_present:
        pytest.skip("patchright is installed here; the not-installed error path "
                    "cannot be exercised in this venv")
    # No browser_factory -> the default factory imports patchright lazily and
    # must raise a clear, actionable install error, not a bare ImportError.
    with pytest.raises(RuntimeError, match=r"pip install patchright==1\.61\.\*"):
        capture_lever("globex", "req-77")
