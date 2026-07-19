"""The browser locale/timezone pin on the kernel's single context factory.

`engine.kernel.capture_toolkit._default_browser_page` is the ONE factory that
creates every browser context in this system. A form's advertised date format
(the `placeholder` on a start-date box) is a property of the browser SESSION,
not of the page -- the same live control returns MM/DD/YYYY under en-US and
DD/MM/YYYY under en-GB. Pinning the one shared factory makes every browser
session this system opens deterministic across hosts and ambient environment:
for the browser-capture vendors (lever, ashby) that means their capture
session and their later fill session agree with each other; for the
HTTP-capture vendors (workable, greenhouse), which never open a browser at
capture time, it means only the fill session is deterministic (see the
comment on `BROWSER_LOCALE` in `capture_toolkit.py` for what that pin does and
does not close for those vendors). Unpinned, a session reads dates in
whatever the host's ambient locale env happens to be, and a wrong-format date
types and reads back cleanly (plain string compare, no `pattern`, no
`maxlength`), so the error is SILENT.

These tests pin the pin, on BOTH context-creation branches, plus a structural
guard (AST, not regex, so a reformat cannot defeat it) that fails if ANY
context-creating call anywhere in the engine-build tree (not just
`engine/kernel/`, and not just `engine/`: also the `w5_accept.py` fill
entrypoint and `bin/`) omits it. That includes `Browser.new_page()`, the
one-call Playwright shortcut that ALSO creates an implicit, unpinned context;
it is only safe when called on a context this guard can prove is already
pinned (see `_receiver_is_pinned_context` below). A vendor plugin, or the fill
entrypoint, could in principle open its own context instead of going through
the shared factory, and the guard must catch that too. They also re-assert
that the structural never-send guard is still installed on the context each
branch yields: the pin must not have displaced it.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pytest

import engine.kernel.never_send
from engine.kernel import capture_toolkit

# `.parents[2]` is the engine-build repo root (not just `engine/`), so the
# walk below also reaches `w5_accept.py` (the live fill entrypoint) and `bin/`.
ENGINE_DIR = Path(capture_toolkit.__file__).parents[2]

# The calls that unconditionally hand back a NEW live BrowserContext. Every
# one of them, anywhere in the engine-build tree, must carry the pin.
_CONTEXT_CREATORS = frozenset({"new_context", "launch_persistent_context"})
_REQUIRED_PIN = {"locale": "en-GB", "timezone_id": "Europe/Rome"}

# `Browser.new_page()` is Playwright's one-call shortcut: it creates an
# implicit BrowserContext AND a Page together, accepting the exact same
# locale/timezone_id kwargs `new_context()` does. It is the shortest path in
# the API and the one every tutorial reaches for. It is safe ONLY when called
# on a receiver this guard can prove is already a pinned Context (e.g. the
# factory's own `context.new_page()`); called on a Browser -- or on anything
# this guard cannot prove is a pinned Context -- it silently opens a SECOND,
# unpinned context, so it is treated below as its own context-creating call
# requiring the pin directly on the `new_page(...)` call.
_PAGE_FACTORY_METHOD = "new_page"


# --------------------------------------------------------------------------- #
# fake patchright: the suite is hermetic; no browser is launched.
# --------------------------------------------------------------------------- #

class FakePage:
    def __init__(self):
        self.timeout = None

    def set_default_timeout(self, ms):
        self.timeout = ms


class FakeContext:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.closed = False

    def new_page(self):
        return FakePage()

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.contexts: list[FakeContext] = []
        self.closed = False

    def new_context(self, **kwargs):
        context = FakeContext(kwargs)
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, record):
        self._record = record

    def launch(self, **kwargs):
        browser = FakeBrowser(kwargs)
        self._record["browser"] = browser
        return browser

    def launch_persistent_context(self, user_data_dir, **kwargs):
        context = FakeContext(kwargs)
        self._record["persistent_user_data_dir"] = user_data_dir
        self._record["persistent_context"] = context
        return context


class FakeController:
    def __init__(self, record):
        self.chromium = FakeChromium(record)


@pytest.fixture
def fake_browser(monkeypatch):
    """Swap the real patchright driver + never-send guard for recording fakes."""
    record: dict = {"never_send_targets": []}

    @contextlib.contextmanager
    def fake_sync_playwright_cm():
        yield FakeController(record)

    monkeypatch.setattr(
        capture_toolkit, "_require_patchright",
        lambda: lambda: fake_sync_playwright_cm())
    monkeypatch.setattr(
        engine.kernel.never_send, "install_never_send",
        lambda context: record["never_send_targets"].append(context))
    return record


def _created_context(record) -> FakeContext:
    """The single context the factory built, whichever branch built it."""
    if "persistent_context" in record:
        return record["persistent_context"]
    return record["browser"].contexts[0]


# --------------------------------------------------------------------------- #
# 1-2. both branches pin locale AND timezone
# --------------------------------------------------------------------------- #

def test_throwaway_branch_pins_locale_and_timezone(fake_browser):
    with capture_toolkit._default_browser_page() as page:
        assert isinstance(page, FakePage)

    context = _created_context(fake_browser)
    assert context.kwargs["locale"] == "en-GB"
    assert context.kwargs["timezone_id"] == "Europe/Rome"


def test_persistent_branch_pins_locale_and_timezone(fake_browser, tmp_path):
    profile = tmp_path / "profile"
    with capture_toolkit._default_browser_page(user_data_dir=profile) as page:
        assert isinstance(page, FakePage)

    assert fake_browser["persistent_user_data_dir"] == str(profile)
    context = fake_browser["persistent_context"]
    assert context.kwargs["locale"] == "en-GB"
    assert context.kwargs["timezone_id"] == "Europe/Rome"


# --------------------------------------------------------------------------- #
# 3. the never-send guard survives the pin, on both branches
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("persistent", [False, True])
def test_never_send_still_installed_on_the_context(fake_browser, tmp_path, persistent):
    kwargs = {"user_data_dir": tmp_path / "profile"} if persistent else {}
    with capture_toolkit._default_browser_page(**kwargs):
        pass

    context = _created_context(fake_browser)
    # Installed on the CONTEXT (a page-scoped route would let a submit escape),
    # and on exactly the context the factory yields a page from.
    assert fake_browser["never_send_targets"] == [context]


# --------------------------------------------------------------------------- #
# 4. structural guard: no context-creating call in the kernel may skip the pin
# --------------------------------------------------------------------------- #

def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level string constants the pin may be expressed as.

    Two forms resolve to a literal: a local `NAME = "literal"` assignment, and
    a name imported straight from `engine.kernel.capture_toolkit` (the pin's
    canonical home) -- so the DRY-correct

        from engine.kernel.capture_toolkit import BROWSER_LOCALE, BROWSER_TIMEZONE_ID
        ...
        browser.new_context(locale=BROWSER_LOCALE, timezone_id=BROWSER_TIMEZONE_ID)

    resolves to the same literal a hardcoded string would, rather than
    steering the next maintainer toward duplicating the literal to satisfy
    this guard. A WRONG import (any other capture_toolkit attribute, or one
    not currently pinned to the expected literal) still resolves to its real,
    non-matching value, so it still fails the check below.
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
        elif isinstance(node, ast.ImportFrom) and node.module == "engine.kernel.capture_toolkit":
            for alias in node.names:
                value = getattr(capture_toolkit, alias.name, None)
                if isinstance(value, str):
                    consts[alias.asname or alias.name] = value
    return consts


def _keyword_string(call: ast.Call, name: str, consts: dict[str, str]):
    """The literal string a keyword resolves to, or None if absent/not literal."""
    for keyword in call.keywords:
        if keyword.arg != name:
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
        if isinstance(keyword.value, ast.Name):
            return consts.get(keyword.value.id)
    return None


def _own_scope_statements(scope: ast.AST):
    """Descendants of `scope` that belong to ITS OWN scope.

    Recurses through ordinary statements (if/for/with/try/...) but does not
    cross into a nested function's body, so a nested def's local bindings
    never leak into the enclosing scope (and vice versa): a `browser`
    PARAMETER of some inner `def` is not the same thing as a `browser`
    variable a sibling scope assigned from `.launch(...)`.
    """
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _own_scope_statements(child)


def _scope_owners(tree: ast.Module) -> dict[int, ast.AST]:
    """id(node) -> the nearest enclosing function-or-module scope."""
    owners: dict[int, ast.AST] = {}

    def visit(node: ast.AST, scope: ast.AST) -> None:
        owners[id(node)] = scope
        inner = node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(tree, tree)
    return owners


def _scope_bindings(scope: ast.AST) -> dict[str, tuple[str, int]]:
    """name -> ("browser" | "context", lineno) for this scope's OWN
    `X = <expr>.launch(...)` / `X = <expr>.new_context(...)` /
    `X = <expr>.launch_persistent_context(...)` assignments.
    """
    bindings: dict[str, tuple[str, int]] = {}
    for node in _own_scope_statements(scope):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call) \
                or not isinstance(node.value.func, ast.Attribute):
            continue
        attr = node.value.func.attr
        if attr == "launch":
            kind = "browser"
        elif attr in _CONTEXT_CREATORS:
            kind = "context"
        else:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = (kind, node.lineno)
    return bindings


def _receiver_is_pinned_context(call: ast.Call, scope_bindings: dict[str, tuple[str, int]]) -> bool:
    """True iff `call`'s receiver is PROVABLY an already-pinned Context.

    Provable means: the receiver is a bare name, assigned earlier in its own
    enclosing scope from `new_context(...)` or `launch_persistent_context(...)`
    (both of which this same guard already requires to carry the pin).
    Anything else -- a `browser.new_page()` receiver bound to `.launch(...)`,
    an unresolved name (e.g. a bare function parameter, as in
    `def _rogue(browser): return browser.new_page()`), or any non-`Name`
    receiver -- is NOT provable safe, so it falls through to being checked as
    its own context-creating call below (fail-closed).
    """
    receiver = call.func.value
    if not isinstance(receiver, ast.Name):
        return False
    binding = scope_bindings.get(receiver.id)
    return binding is not None and binding[0] == "context" and binding[1] < call.lineno


def test_every_kernel_context_creation_carries_the_pin():
    """Fails if ANY context-creating call in the engine-build tree omits
    locale/timezone.

    AST-based on purpose: a reformat, a rename of the local variable, or a
    move to another module in the tree (kernel, vendor plugin, or the
    `w5_accept.py` / `bin/` entrypoints) cannot slip a bare `new_context()` --
    or a bare `browser.new_page()`, Playwright's implicit-context shortcut --
    past this.
    """
    checked = 0
    for pyfile in sorted(ENGINE_DIR.rglob("*.py")):
        tree = ast.parse(pyfile.read_text(), filename=str(pyfile))
        consts = _module_string_constants(tree)
        owners = _scope_owners(tree)
        bindings_by_scope: dict[int, dict[str, tuple[str, int]]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr
            if attr == _PAGE_FACTORY_METHOD:
                scope = owners[id(node)]
                scope_bindings = bindings_by_scope.setdefault(id(scope), _scope_bindings(scope))
                if _receiver_is_pinned_context(node, scope_bindings):
                    continue  # new_page() on an already-pinned context: safe.
            elif attr not in _CONTEXT_CREATORS:
                continue
            checked += 1
            where = f"{pyfile.name}:{node.lineno} ({attr})"
            for kwarg, expected in _REQUIRED_PIN.items():
                assert _keyword_string(node, kwarg, consts) == expected, (
                    f"{where} must pass {kwarg}={expected!r}: an unpinned browser "
                    f"context reads the ambient host locale, and the date format "
                    f"it derives can silently disagree with the fill session's.")

    # Guard the guard: if the factory were deleted or renamed away, the loop above
    # would pass vacuously.
    assert checked >= 2, (
        f"expected both context-creating branches in {ENGINE_DIR}, found {checked}")
