"""ATS browser field-map capture via headless Playwright (W4 3.2).

Reaches the two vendors that a browserless HTTP probe cannot enumerate:

- Ashby: the posting page is a SPA that fetches its typed application schema
  through its own `non-user-graphql` (ApiJobPosting) call. We load the page,
  intercept that response, and map its field definitions onto the canonical
  FieldMap. The typed schema lives at `data.jobPosting.applicationForm` (a
  `FormRender`, confirmed live 2026-07-03); the older
  `data.jobPosting.applicationFormDefinition` shape is probed second as a
  one-release fallback. Ashby forms carry NO conditional visibility (R-WT-8 8):
  each section is a plain linear step, so `step_index` increments once per
  visible section (hidden sections are dropped whole) and never branches.
- Lever: the apply page is server-rendered, so the full form DOM is present at
  load. We read the rendered DOM (`page.content()`) and parse the fixed base
  fields plus the custom `.application-question` cards.

Both paths return the SAME canonical FieldMap schema produced by fieldmap.py
(vendor, posting_id, schema_version, captured_at, fields[...]). `captured_at` is
stamped now; the posting `updated_at` cache key is the caller's concern (run.py
passes it to the store), not this module's.

Egress + politeness (R-WT-8 A/B/D3): headless chromium running DIRECTLY on toto
(not inside gluetun), one anonymous context per capture (no storage state, no
cookies persisted), exactly ONE page load per capture, 20s timeouts, and no
retry beyond that single load. NO clicks, NO login, NO submission: this wave is
read-only.

Import guard: this module imports cleanly WITHOUT the browser driver installed
(the engine core stays stdlib+PyYAML). Patchright is imported lazily, only when a
capture is actually invoked with the default (real) browser factory; invoking it
without patchright raises a clear "pip install patchright==1.61.*" error. Tests
drive both paths with fake browser/page objects and never touch patchright or the
network.

CAVEAT (load-bearing): the Ashby graphql envelope was confirmed LIVE against
jobs.ashbyhq.com during the round-2 SSOT playtest loop (2026-07-03): the real
schema lives at `data.jobPosting.applicationForm`, not the fixture-derived
`applicationFormDefinition` guessed in round 1 (kept as a one-release
fallback probe). Every parser MUST still fail LOUDLY and descriptively: a
shape mismatch raises `CaptureShapeError` naming the exact selector/key that
missed, NEVER a silently empty FieldMap. Callers stay fail-soft (run.py counts
a failed capture and moves on) so a loud parser and a resilient runner
coexist.

ROUND-3 LIVE FINDING (jobs.lever.co, 2026-07-03): the Lever DOM selectors
above were confirmed against a real apply page, with two shape corrections.
First, each base field renders TWICE: an invisible mirror carrying the true
submission `name` with no label, plus a labeled visible twin (`Full name`,
`Email`, ...). `_dedup_lever_base_fields` collapses each pair back into one
logical Field (human label, OR'd `required`, richer-source `type`) keyed by
`_LEVER_BASE_LABELS`, so callers never see a duplicated field. Second, a
custom question card can render its text inline (e.g. a consent checkbox
whose wording sits beside the input rather than in a `.application-label`
div); `_resolve_field_label` tries `aria-label`, `placeholder`, and the
enclosing element's own text before ever emitting an empty label.
"""

from __future__ import annotations

# Generic browser/HTML capture infra: moved to the kernel (W5.1 stage 5).
# Re-exported below for back-compat (`browse.X` attribute access from
# fill.py's lazy factory import, w5_accept.py, test_browse.py, and any future
# vendor plugin that still reaches this module during the Stage 2 split).
from engine.kernel.capture_toolkit import (  # noqa: F401
    PATCHRIGHT_PIN, _TIMEOUT_MS, CaptureShapeError,
    _require_patchright, _headless_default, _default_browser_page,
    _Node, _TreeBuilder, _build_tree, _find_all, _first, _has_class,
    _node_text, _VOID_TAGS, _dig, _response_url, _now, _utc_now_iso,
)


# -- ashby capture re-exports (W5.1 Stage 2c dedupe) ---------------------------
# The Ashby graphql capture/parse code MOVED to `engine.providers.ashby.capture`
# (single-source: each name is now defined ONCE, in the ashby package). Its
# transitive closure is DISJOINT from the Lever DOM parse (itself carved to
# `engine.providers.lever.capture` in Stage 2d): NOTHING was left shared, the
# Ashby path reads the intercepted graphql JSON and touches none of the
# label/text/tree helpers (all generic browser/HTML infra it uses is
# single-sourced in `engine.kernel.capture_toolkit` / `engine.kernel.contracts`,
# imported straight from the kernel there). The
# moved names are re-exported here via a LAZY module `__getattr__` (PEP 562),
# mirroring the `engine.fieldmap` / `engine.providers.base` greenhouse shims, so
# every pre-Stage-2 importer keeps resolving them via `engine.browse` unchanged:
#   * `registry._capture_ashby` / `_apply_ashby`'s call-time `browse.capture_
#     ashby` / `browse.ashby_application_url` module-attribute lookups;
#   * `engine.fill._capture`'s call-time `from engine.browse import capture_ashby`
#     (a from-import IS attribute access, so it triggers this __getattr__);
#   * the tests' `from engine.browse import ASHBY_SOURCE, capture_ashby`,
#     `browse._parse_ashby`, and `monkeypatch.setattr(browse, "capture_ashby",
#     ...)` (setattr binds a REAL attribute that shadows this __getattr__;
#     teardown restores, after which __getattr__ serves the moved object again).
# The import is DEFERRED to attribute-access time (NEVER at browse load), which
# keeps `import engine.browse` from eagerly pulling the ashby package (and keeps
# browse's browser-free load invariant intact); it also cannot cycle should the
# ashby package ever reach back into `engine.browse`.
_ASHBY_CAPTURE_NAMES = frozenset({
    "capture_ashby", "ashby_application_url", "ASHBY_SOURCE",
    "_ASHBY_GRAPHQL_MARKER", "_ASHBY_TYPE_MAP", "_maybe_capture",
    "_select_ashby_schema", "_parse_ashby", "_parse_ashby_form_render",
    "_parse_ashby_form_definition", "_build_ashby_field", "_ashby_field_type",
    "_ashby_options",
})

# -- lever capture re-exports (W5.1 Stage 2d dedupe) ---------------------------
# The Lever apply-DOM capture/parse code MOVED to `engine.providers.lever.capture`
# (single-source: each name is now defined ONCE, in the lever package). Its
# transitive closure is DISJOINT from the Ashby graphql parse above: Lever reads
# the server-rendered HTML tree, Ashby the intercepted graphql JSON, so the two
# share NO helper -- and neither shares one with what remains here (all generic
# browser/HTML infra both use is single-sourced in `engine.kernel.capture_toolkit`
# / `engine.kernel.contracts`). The moved names are re-exported via the SAME LAZY
# module `__getattr__` (PEP 562) as Ashby, a parallel name-set, so every
# pre-Stage-2 importer keeps resolving them via `engine.browse` unchanged:
#   * `registry._capture_lever` / `_apply_lever`'s call-time `browse.capture_
#     lever` / `browse.lever_apply_url` module-attribute lookups;
#   * `engine.fill._capture`'s call-time `from engine.browse import capture_lever`
#     (a from-import IS attribute access, so it triggers this __getattr__);
#   * the tests' `from engine.browse import LEVER_SOURCE, capture_lever`,
#     `browse._parse_lever`, and `monkeypatch.setattr(browse, "capture_lever",
#     ...)` (setattr binds a REAL attribute that shadows this __getattr__;
#     teardown restores, after which __getattr__ serves the moved object again).
# DEFERRED to attribute-access time (NEVER at browse load), keeping `import
# engine.browse` from eagerly pulling the lever package and preserving browse's
# browser-free load invariant; it also cannot cycle should the lever package
# ever reach back into `engine.browse`.
_LEVER_CAPTURE_NAMES = frozenset({
    "capture_lever", "lever_apply_url", "LEVER_SOURCE", "_LEVER_BASE_LABELS",
    "_parse_lever", "_dedup_by_key", "_RawBaseField", "_lever_base_fields",
    "_dedup_lever_base_fields", "_merge_lever_groups_by_normalized_label",
    "_merge_lever_base_group", "_pick_lever_label", "_richer_lever_type",
    "_lever_type_rank", "_lever_custom_fields", "_lever_custom_field",
    "_resolve_field_label", "_is_form_control", "_control_type",
    "_control_label", "_is_required", "_select_options", "_checkbox_label",
    "_slug",
})


def __getattr__(name):
    if name in _ASHBY_CAPTURE_NAMES:
        import importlib
        return getattr(
            importlib.import_module("engine.providers.ashby.capture"), name)
    if name in _LEVER_CAPTURE_NAMES:
        import importlib
        return getattr(
            importlib.import_module("engine.providers.lever.capture"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
