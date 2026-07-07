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

import re
from dataclasses import dataclass
from typing import Callable

from engine.fieldmap import Field, FieldMap, Locator, _role_for_type

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

LEVER_SOURCE = "lever_dom"

# Lever base-field `name` -> canonical human label (round-3 finding): the live
# apply page renders every base field TWICE, an invisible mirror carrying the
# true submission `name` with no label at all, plus a labeled visible twin.
# `_dedup_lever_base_fields` collapses each pair into one logical Field and
# uses this table to recover the human label when neither duplicate happens
# to carry one.
_LEVER_BASE_LABELS = {
    # Round-4 live finding: the apply page also carries a base `location`
    # input (autocomplete widget) whose visible twin is "Current location";
    # without this entry it escaped dedup and its label extraction grabbed
    # the widget's error text.
    "location": "Current location",
    "name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "org": "Current company",
    "urls[LinkedIn]": "LinkedIn URL",
    "urls[Twitter]": "Twitter URL",
    "urls[GitHub]": "GitHub URL",
    "urls[Portfolio]": "Portfolio URL",
    "urls[Other]": "Other URL",
    "resume": "Resume / CV",
}


def lever_apply_url(slug: str, job_id: str) -> str:
    return f"https://jobs.lever.co/{slug}/{job_id}/apply"


def capture_lever(slug: str, job_id: str, browser_factory=None, *,
                  now: Callable[[], str] | None = None) -> FieldMap:
    """Capture one Lever posting's field map from the server-rendered apply DOM.

    Loads the apply page once and reads the rendered DOM via `page.content()`
    (server-rendered: no interception needed), then parses the fixed base fields
    plus the custom `.application-question` cards (`source="lever_dom"`). A
    read-only page load, so Lever's POST rate limits never apply.
    """
    factory = browser_factory or _default_browser_page
    url = lever_apply_url(slug, job_id)
    with factory() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        html_source = page.content()
    return _parse_lever(html_source, slug, job_id, now=now)


# -- Lever DOM parse -----------------------------------------------------------

def _parse_lever(html_source: str, slug: str, job_id: str, *,
                 now: Callable[[], str] | None = None) -> FieldMap:
    tree = _build_tree(html_source)
    fields = _dedup_by_key(
        _lever_base_fields(tree) + _lever_custom_fields(tree, slug, job_id))
    if not fields:
        raise CaptureShapeError(
            f"lever: the apply page for {slug}/{job_id} rendered no recognizable "
            "form fields (no .application-field or .application-question blocks "
            "found); the DOM shape has drifted or the page did not load")
    return FieldMap(vendor="lever", posting_id=str(job_id),
                    captured_at=_now(now), fields=fields)


def _dedup_by_key(fields: list) -> list:
    """Final whole-map same-key collapse (round-4 live finding): the base and
    custom parse paths can BOTH emit a field for the same submission key on
    live pages (the fixture-level dedup only collapses within the base pass).
    Keep the first occurrence, upgrading required to the OR across duplicates
    and preferring a non-empty label."""
    by_key: dict[str, object] = {}
    for fld in fields:
        kept = by_key.get(fld.key)
        if kept is None:
            by_key[fld.key] = fld
            continue
        kept.required = kept.required or fld.required
        if not kept.label and fld.label:
            kept.label = fld.label
    return list(by_key.values())


@dataclass
class _RawBaseField:
    """One `.application-field` container's parse, pre-dedup."""
    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    container: "_Node"
    control: "_Node"


def _lever_base_fields(tree: "_Node") -> list[Field]:
    """The fixed base fields: every input inside an `.application-field` block.

    The live apply page renders each base field TWICE (round-3 finding): an
    invisible mirror carrying the true submission `name` with no label, and a
    labeled visible twin. Both parse to the SAME `key` here, so
    `_dedup_lever_base_fields` collapses each duplicate pair back into ONE
    logical Field before returning.
    """
    raw: list[_RawBaseField] = []
    for container in _find_all(tree, lambda n: _has_class(n, "application-field")):
        control = _first(container, _is_form_control)
        if control is None:
            continue
        name = control.attrs.get("name", "")
        if not name:
            continue
        raw.append(_RawBaseField(
            key=name,
            label=_control_label(container),
            type=_control_type(control),
            required=_is_required(container, control),
            options=_select_options(control),
            container=container,
            control=control,
        ))
    return _dedup_lever_base_fields(raw)


def _dedup_lever_base_fields(raw: list[_RawBaseField]) -> list[Field]:
    groups: dict[str, list[_RawBaseField]] = {}
    order: list[str] = []
    for item in raw:
        if item.key not in groups:
            order.append(item.key)
        groups.setdefault(item.key, []).append(item)
    _merge_lever_groups_by_normalized_label(groups, order)
    fields: list[Field] = []
    for key in order:
        items = groups.get(key)
        if items is None:
            continue  # merged away into another key's group
        fields.append(_merge_lever_base_group(key, items))
    return fields


def _merge_lever_groups_by_normalized_label(groups: dict, order: list[str]) -> None:
    """Fallback normalized-label match: collapse two DIFFERENT keys into one
    group when both are label-bearing singletons whose labels normalize to
    the same slug. Covers a base/labeled pair whose visible twin renders
    under a different `name` than its hidden mirror -- not observed live yet
    for Lever, kept defensive since the same duplication shape recurred once
    already (round-2 Ashby shape drift) and could recur here too."""
    seen: dict[str, str] = {}
    for key in list(order):
        items = groups.get(key)
        if items is None or len(items) != 1 or not items[0].label:
            continue
        slug = _slug(items[0].label)
        primary = seen.get(slug)
        if primary is None:
            seen[slug] = key
        elif primary != key:
            groups[primary].extend(items)
            del groups[key]


def _merge_lever_base_group(key: str, items: list[_RawBaseField]) -> Field:
    label = _pick_lever_label(key, items)
    required = any(item.required for item in items)
    field_type, options = _richer_lever_type(items)
    return Field(
        key=key,
        label=label,
        type=field_type,
        required=required,
        options=options,
        source=LEVER_SOURCE,
        locator=Locator(role=_role_for_type(field_type), name=label),
        step_index=0,
        conditional_on=None,
    )


def _pick_lever_label(key: str, items: list[_RawBaseField]) -> str:
    """Keep the human label: the first duplicate that actually carries one,
    else the deterministic base/label table, else the harder-extraction
    fallback chain (never an empty string, per round-3 item 2)."""
    for item in items:
        if item.label:
            return item.label
    if key in _LEVER_BASE_LABELS:
        return _LEVER_BASE_LABELS[key]
    return _resolve_field_label("", items[0].container, items[0].control, key)


def _richer_lever_type(items: list[_RawBaseField]) -> tuple[str, list[str]]:
    """Type from the richer source: an option-carrying or more specific
    control (e.g. a `type=file` upload widget) outranks the plain-text
    default that a hidden mirror typically parses as."""
    best = items[0]
    best_rank = _lever_type_rank(best)
    for item in items[1:]:
        rank = _lever_type_rank(item)
        if rank > best_rank:
            best, best_rank = item, rank
    return best.type, best.options


def _lever_type_rank(item: _RawBaseField) -> int:
    if item.options:
        return 4
    if item.type == "input_file":
        return 3
    if item.type == "boolean":
        return 2
    if item.type == "textarea":
        return 1
    return 0


def _lever_custom_fields(tree: "_Node", slug: str, job_id: str) -> list[Field]:
    return [_lever_custom_field(card, slug, job_id)
            for card in _find_all(tree,
                                  lambda n: _has_class(n, "application-question"))]


def _lever_custom_field(card: "_Node", slug: str, job_id: str) -> Field:
    raw_label = _control_label(card)
    controls = [n for n in _find_all(card, _is_form_control)
                if (n.attrs.get("type") or "").lower() != "hidden"]
    if not controls:
        raise CaptureShapeError(
            f"lever: custom question card {raw_label!r} on {slug}/{job_id} has no "
            "input/select/textarea control")
    checkboxes = [n for n in controls
                  if (n.attrs.get("type") or "").lower() == "checkbox"]
    if len(checkboxes) > 1:
        field_type = "multi_value_multi_select"
        options = [_checkbox_label(cb) for cb in checkboxes]
        primary = checkboxes[0]
        key = primary.attrs.get("name", "") or _slug(raw_label)
    else:
        primary = controls[0]
        field_type = _control_type(primary)
        options = _select_options(primary)
        key = primary.attrs.get("name", "") or _slug(raw_label)
    label = _resolve_field_label(raw_label, card, primary, key)
    return Field(
        key=key,
        label=label,
        type=field_type,
        required=_is_required(card, controls[0]),
        options=options,
        source=LEVER_SOURCE,
        locator=Locator(role=_role_for_type(field_type), name=label),
        step_index=0,
        conditional_on=None,
    )


def _resolve_field_label(label: str, container: "_Node", control: "_Node",
                         key: str) -> str:
    """Harder label-extraction fallback chain (round-3 item 2): a captured
    field must never carry an empty label. Tried in order: the caller's own
    `.application-label` read (`label`, already attempted before this is
    called), `aria-label`, `placeholder`, the enclosing element's own trimmed
    text (e.g. a consent checkbox whose wording sits inline rather than in a
    dedicated label element), and finally the field's key as a last resort."""
    if label:
        return label
    aria = (control.attrs.get("aria-label") or "").strip()
    if aria:
        return aria
    placeholder = (control.attrs.get("placeholder") or "").strip()
    if placeholder:
        return placeholder
    enclosing = _node_text(container, exclude_cls="required")
    if enclosing:
        return enclosing
    return f"(unlabeled: {key})" if key else "(unlabeled)"


def _is_form_control(node: "_Node") -> bool:
    return node.tag in ("input", "textarea", "select")


def _control_type(control: "_Node") -> str:
    if control.tag == "textarea":
        return "textarea"
    if control.tag == "select":
        return "multi_value_single_select"
    input_type = (control.attrs.get("type") or "text").lower()
    if input_type == "file":
        return "input_file"
    if input_type == "checkbox":
        return "boolean"
    return "input_text"


def _control_label(container: "_Node") -> str:
    label_node = _first(container, lambda n: _has_class(n, "application-label"))
    if label_node is None:
        return ""
    return _node_text(label_node, exclude_cls="required")


def _is_required(container: "_Node", control: "_Node") -> bool:
    if control is not None and "required" in control.attrs:
        return True
    return _first(container, lambda n: _has_class(n, "required")) is not None


def _select_options(control: "_Node") -> list[str]:
    if control.tag != "select":
        return []
    options: list[str] = []
    for option in _find_all(control, lambda n: n.tag == "option"):
        value = option.attrs.get("value", "")
        text = _node_text(option)
        if value == "" and (not text or text.lower().startswith("select")):
            continue  # the "Select..." placeholder is not a real option
        options.append(text or value)
    return options


def _checkbox_label(checkbox: "_Node") -> str:
    return checkbox.attrs.get("value", "") or checkbox.attrs.get("name", "")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "field"


# -- ashby capture re-exports (W5.1 Stage 2c dedupe) ---------------------------
# The Ashby graphql capture/parse code MOVED to `engine.providers.ashby.capture`
# (single-source: each name is now defined ONCE, in the ashby package). Its
# transitive closure is DISJOINT from the Lever DOM parse that stays here, so
# NOTHING was left shared: the Ashby path reads the intercepted graphql JSON and
# touches none of browse.py's label/text/tree helpers (all generic
# browser/HTML infra it uses is single-sourced in `engine.kernel.capture_toolkit`
# / `engine.kernel.contracts`, imported straight from the kernel there). The
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


def __getattr__(name):
    if name in _ASHBY_CAPTURE_NAMES:
        import importlib
        return getattr(
            importlib.import_module("engine.providers.ashby.capture"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
