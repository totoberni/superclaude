"""Lever apply-DOM capture + parse (W5.1 Stage 2d; moved from engine.browse).

Lever is the server-rendered vendor: the apply page ships the full form DOM at
load, so there is nothing to intercept. `capture_lever` loads the page once,
reads the rendered DOM via `page.content()`, and parses the fixed base fields
plus the custom `.application-question` cards onto the canonical `FieldMap`
(`source="lever_dom"`). Every parser fails LOUDLY: a form that yields no
recognizable fields raises `CaptureShapeError` naming the shape that missed,
NEVER a silently empty FieldMap.

ROUND-3/4 LIVE FINDINGS carried over verbatim (jobs.lever.co, 2026-07-03):
each base field renders TWICE (an invisible mirror carrying the true submission
`name` with no label, plus a labeled visible twin); `_dedup_lever_base_fields`
+ the whole-map `_dedup_by_key` collapse each pair back into one logical Field
(human label, OR'd `required`, richer-source `type`). A custom question card can
render its wording inline (e.g. a consent checkbox beside the input rather than
in a `.application-label`), so `_resolve_field_label` tries `aria-label`,
`placeholder`, and the enclosing element's own text before ever emitting an
empty label.

Only the LEVER-specific capture/parse code moved here. Its transitive closure is
DISJOINT from Ashby's graphql parse (which reads the intercepted JSON and touches
no HTML tree): the two vendors share NONE of each other's helpers, so nothing was
left behind to import from `engine.browse`. Everything this module reaches beyond
its own helpers is generic browser/HTML INFRA already single-sourced in the
kernel -- the browser page factory + timeout + tree builder/finders + node-text
reader + `CaptureShapeError` + `_now` from `engine.kernel.capture_toolkit`, and
the `Field`/`FieldMap`/`Locator` contracts + `_role_for_type` from
`engine.kernel.contracts` -- so nothing is re-implemented and there is exactly
one home for each name.

`engine.browse` keeps a LAZY re-export shim (PEP 562 `__getattr__`) for every
name moved here, so existing importers keep resolving them via `engine.browse`
unchanged: `registry._capture_lever` / `_apply_lever`'s call-time
`browse.capture_lever` / `browse.lever_apply_url`, `engine.fill._capture`'s
call-time `from engine.browse import capture_lever`, and the tests'
`from engine.browse import LEVER_SOURCE, capture_lever`, `browse._parse_lever`,
and `monkeypatch.setattr(browse, "capture_lever", ...)` seam.

LAZY-IMPORT INVARIANT (mirrors browse.py / base.py / registry.py): patchright is
imported lazily inside `_default_browser_page` (in the kernel), only when a real
capture runs, so importing this module -- and importing `engine.providers.lever`
-- stays browser-free for the daily poller. Tests drive the parse path over
fixture DOM through a fake browser/page factory and never touch patchright or the
network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from engine.kernel.capture_toolkit import (
    _TIMEOUT_MS,
    CaptureShapeError,
    _build_tree,
    _default_browser_page,
    _find_all,
    _first,
    _has_class,
    _node_text,
    _Node,
    _now,
)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    Locator,
    _role_for_type,
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
