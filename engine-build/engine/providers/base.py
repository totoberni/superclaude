"""Shared browser primitives every ATS provider builds on (W5.1 spine).

This module is the single home for the cross-vendor fill mechanics that the
per-vendor providers (greenhouse/lever/ashby/workable, landing in W5.2) reuse:
the four live fill primitives from `engine.kernel.fill_toolkit`, a STRUCTURAL
never-send network interceptor, human-cadence typing, and a DOM-sweep
completeness check.

LAZY-IMPORT INVARIANT (load-bearing, mirrors registry.py): the daily poller must
never load the browser stack. `engine.run` imports `engine.providers` (the package
__init__, which pulls in `registry` only), NOT this module, and this module never
imports patchright or any browser-capture module at load time. Every browser reference here is
resolved through the page/locator/route objects the caller passes in; the only
cross-module import (`engine.kernel.fill_toolkit`, itself patchright-free) happens
at CALL time inside the re-export wrappers.

FILL-PRIMITIVE ACCESS -- re-export via call-time wrappers, NOT a top-level
`from engine.kernel.fill_toolkit import ...`:
- The four primitives live in the kernel (`engine.kernel.fill_toolkit`); they are
  surfaced here by thin pass-through wrappers so provider fill code keeps calling
  `base._safe_click` / `base._readback` / etc.
- The wrappers look the target up on the `engine.kernel.fill_toolkit` module object
  at call time, so they honour the monkeypatch seam (a test patching
  `engine.kernel.fill_toolkit._safe_click` is reflected here), the same
  call-time-lookup discipline the vendor capture plugins use for their own
  monkeypatch seams. A top-level `from engine.kernel.fill_toolkit import _safe_click`
  would bind the reference at import and defeat that seam.

The NEW primitives (`install_never_send`, `type_human`, `sweep_required` +
`completeness_mismatch`) are pure-Python in the kernel: they drive whatever
page/locator/route object is handed to them, so their branching logic is
unit-tested with fakes and their live-DOM behaviour is fixture-validated in W5.2.

GREENHOUSE WIDGET CLUSTER (W5.1 Stage 2a): the react-select combobox driver and
the resume-upload rendered-confirmation poll -- greenhouse-only DOM widgets, not
cross-vendor primitives -- MOVED to `engine.providers.greenhouse.fill`. They are
re-exported here via a lazy module `__getattr__` (bottom of file) so existing
`base.select_react_combobox` / `base.poll_upload_confirmed` call sites and unit
tests keep working unchanged.
"""

from __future__ import annotations

# never-send guard: FROZEN, single-source in the kernel (W5.1). Re-exported for back-compat.
from engine.kernel.never_send import (  # noqa: F401
    _SUBMIT_URL_PATTERNS, _SUBMIT_GRAPHQL_URL_PATTERNS, _SUBMIT_OPERATION_RE, _GRAPHQL_MUTATION_RE,
    _graphql_operation_names, _url_op_params, _all_ops_carry_inline_query, _graphql_submit_match,
    _is_submit_request, _never_send_handler, _request_post_data, install_never_send, _route_target,
)

# generic form-driving primitives: moved to the kernel (W5.1). Re-exported for back-compat + monkeypatch seam.
from engine.kernel.fill_toolkit import (  # noqa: F401
    type_human, _settle_focus, _normalize_name, completeness_mismatch, sweep_required,
    _visible_locators, _is_visible, _is_aria_hidden, _accessible_name, _locator_text,
    _REQUIRED_CSS, _ASTERISK_CSS,
)

# -- re-exported fill primitives (call-time lookup preserves the patch seam) ----


def _toolkit():
    """The `engine.kernel.fill_toolkit` module, resolved fresh on every call so
    the wrappers honour the monkeypatch seam (a test patching
    `engine.kernel.fill_toolkit._safe_click` is reflected through them)."""
    from engine.kernel import fill_toolkit
    return fill_toolkit


def _safe_click(*args, **kwargs):
    """Re-export of `engine.kernel.fill_toolkit._safe_click` (the sole sanctioned
    click gateway; refuses any submit-like accessible name)."""
    return _toolkit()._safe_click(*args, **kwargs)


def _safe_upload(*args, **kwargs):
    """Re-export of `engine.kernel.fill_toolkit._safe_upload` (whitelisted-asset
    attach; never submits)."""
    return _toolkit()._safe_upload(*args, **kwargs)


def _readback(*args, **kwargs):
    """Re-export of `engine.kernel.fill_toolkit._readback` (reads a control back to
    confirm a value actually landed)."""
    return _toolkit()._readback(*args, **kwargs)


def _locate(*args, **kwargs):
    """Re-export of `engine.kernel.fill_toolkit._locate` (role/label locator
    resolution)."""
    return _toolkit()._locate(*args, **kwargs)


# -- STRUCTURAL never-send (HOLE-FIX a): moved to engine.kernel.never_send, frozen
# byte-identical (W5.1 stage 0). Re-exported above for back-compat.

# -- generic form-driving primitives (human-cadence typing, DOM-sweep completeness):
# moved to engine.kernel.fill_toolkit (W5.1). Re-exported above for back-compat.


# -- greenhouse react-select / upload widget cluster: MOVED to
# engine.providers.greenhouse.fill (W5.1 Stage 2a) ----------------------------
# These are greenhouse-only DOM widgets (react-select combobox driver +
# resume-upload rendered-confirmation poll), not cross-vendor primitives. They
# are re-exported here via a LAZY module __getattr__ (PEP 562) so:
#   * the greenhouse fill() orchestration keeps calling `base.select_react_
#     combobox` / `base.poll_upload_confirmed` at its own module-attr call
#     sites -- preserving the `monkeypatch.setattr(base, "poll_upload_
#     confirmed", ...)` test seam -- even though the functions now live in
#     greenhouse.fill; and
#   * the base unit tests keep reaching `base.select_react_combobox`,
#     `base._combobox_control_selector`, `base._settle_event_loop`, etc.
# The import is deferred to attribute-access time (NEVER at base load), so it
# cannot form an import cycle with greenhouse.fill (which imports `base` at its
# own load). A name monkeypatched onto `base` becomes a real attribute that
# shadows this __getattr__, so the patch routes; teardown restores the real
# object (which __getattr__ would return anyway).

_GREENHOUSE_WIDGET_NAMES = frozenset({
    "select_react_combobox", "poll_upload_confirmed",
    "_combobox_control_selector", "_combobox_control", "_combobox_input",
    "_keyboard_press", "_poll_single_value", "_single_value_text",
    "_wait_timeout", "_REMOVE_CONTROL_NAME_RE", "_settle_event_loop",
    "_page_shows_filename", "_upload_widget_confirmed",
    "_upload_widget_container", "_has_remove_control",
})


def __getattr__(name):
    if name in _GREENHOUSE_WIDGET_NAMES:
        import importlib
        return getattr(
            importlib.import_module("engine.providers.greenhouse.fill"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
