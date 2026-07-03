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

Import guard: this module imports cleanly WITHOUT playwright installed (the
engine core stays stdlib+PyYAML). playwright is imported lazily, only when a
capture is actually invoked with the default (real) browser factory; invoking it
without playwright raises a clear "pip install playwright==1.56.*" error. Tests
drive both paths with fake browser/page objects and never touch playwright or the
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

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable

from engine.fieldmap import Field, FieldMap, Locator, _role_for_type

# Pinned per R-WT-8 A (1.57 carried a memory regression); surfaced verbatim in
# the not-installed error so an operator copies the exact remediation.
PLAYWRIGHT_PIN = "playwright==1.56.*"

_TIMEOUT_MS = 20_000

ASHBY_SOURCE = "ashby_graphql"
LEVER_SOURCE = "lever_dom"

# The SPA response we intercept: the substring that marks Ashby's own typed-form
# graphql call among every response the posting page fires.
_ASHBY_GRAPHQL_MARKER = "non-user-graphql"

# Ashby ApiJobPosting field `type` -> canonical FieldMap type. The a11y role is
# then derived from the canonical type via fieldmap's single source of truth
# (_role_for_type), so browser and HTTP captures never drift on role naming.
_ASHBY_TYPE_MAP = {
    "String": "input_text",
    "LongText": "textarea",
    "Email": "input_text",
    "Phone": "input_text",
    "Number": "input_text",
    "Boolean": "boolean",
    "ValueSelect": "multi_value_single_select",
    "MultiValueSelect": "multi_value_multi_select",
    "File": "input_file",
    "Date": "input_text",
}

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

# HTML elements with no end tag; the tree builder must not expect to pop them.
_VOID_TAGS = frozenset({
    "input", "br", "img", "hr", "meta", "link", "source", "col", "area",
    "base", "embed", "param", "track", "wbr",
})


class CaptureShapeError(RuntimeError):
    """A live DOM/graphql shape did not match this fixture-derived parser.

    Raised (never swallowed into an empty FieldMap) with the exact selector or
    key that missed, so a shape drift surfaces loudly during the SSOT playtest
    loop instead of quietly reporting an empty form. The runner catches it and
    counts the item as a failed capture (fail-soft).
    """


def ashby_application_url(slug: str, job_id: str) -> str:
    return f"https://jobs.ashbyhq.com/{slug}/{job_id}/application"


def lever_apply_url(slug: str, job_id: str) -> str:
    return f"https://jobs.lever.co/{slug}/{job_id}/apply"


def capture_ashby(slug: str, job_id: str, browser_factory=None, *,
                  now: Callable[[], str] | None = None) -> FieldMap:
    """Capture one Ashby posting's field map via graphql response interception.

    Loads the posting page once, intercepts the `non-user-graphql` response that
    carries the ApiJobPosting form schema, and maps its field definitions onto
    the canonical FieldMap (`source="ashby_graphql"`). `browser_factory` is an
    injectable returning a context manager that yields a page (a real headless
    chromium page in production, a fake in tests); None builds the real one.
    """
    factory = browser_factory or _default_browser_page
    url = ashby_application_url(slug, job_id)
    with factory() as page:
        captured: list = []
        page.on("response", lambda response: _maybe_capture(captured, response))
        page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
        posting = _select_ashby_schema(captured, slug, job_id)
    return _parse_ashby(posting, slug, job_id, now=now)


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


# -- playwright lifecycle (lazy import; the only place playwright is touched) ---

def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed, so the browser field-map capture path "
            f"is unavailable. Install the pinned build: pip install "
            f"{PLAYWRIGHT_PIN} && playwright install chromium --with-deps"
        ) from exc
    return sync_playwright


@contextlib.contextmanager
def _default_browser_page():
    """Yield a headless chromium page in a fresh anonymous context.

    One context per capture, no storage state and no persisted cookies (the
    default `new_context()` is stateless), torn down unconditionally on exit.
    """
    sync_playwright = _require_playwright()
    with sync_playwright() as controller:
        browser = controller.chromium.launch(headless=True)
        try:
            context = browser.new_context()  # anonymous: no storage_state, no cookies
            try:
                page = context.new_page()
                page.set_default_timeout(_TIMEOUT_MS)
                yield page
            finally:
                context.close()
        finally:
            browser.close()


# -- Ashby graphql parse -------------------------------------------------------

def _maybe_capture(captured: list, response) -> None:
    if _ASHBY_GRAPHQL_MARKER in _response_url(response):
        captured.append(response)


def _select_ashby_schema(responses: list, slug: str, job_id: str) -> dict:
    """Pick the intercepted graphql response carrying the ApiJobPosting form.

    The typed schema lives at `data.jobPosting.applicationForm` (live-confirmed
    2026-07-03); `data.jobPosting.applicationFormDefinition` is probed second
    as a one-release fallback for postings still served the pre-migration
    shape. Whichever key is present and truthy on a response's `jobPosting`
    wins. If none of the matching-URL responses carry either key, the key set
    actually seen on each is recorded so the raise below names both paths
    tried alongside exactly what shape came back.
    """
    seen_keys: list[list[str]] = []
    for response in responses:
        try:
            body = response.json()
        except Exception:
            continue
        posting = _dig(body, "data", "jobPosting")
        if not isinstance(posting, dict):
            continue
        seen_keys.append(sorted(posting.keys()))
        if posting.get("applicationForm") or posting.get("applicationFormDefinition"):
            return posting
    raise CaptureShapeError(
        f"ashby: no {_ASHBY_GRAPHQL_MARKER!r} response for {slug}/{job_id} "
        "carried data.jobPosting.applicationForm or the fallback "
        "data.jobPosting.applicationFormDefinition "
        f"(saw {len(responses)} matching-URL response(s); jobPosting keys "
        f"seen: {seen_keys}); the graphql shape has drifted or the form "
        "never loaded")


def _parse_ashby(posting: dict, slug: str, job_id: str, *,
                 now: Callable[[], str] | None = None) -> FieldMap:
    form = posting.get("applicationForm")
    if isinstance(form, dict) and form:
        fields = _parse_ashby_form_render(form, slug, job_id)
    else:
        definition = posting.get("applicationFormDefinition")
        definition = definition if isinstance(definition, dict) else {}
        fields = _parse_ashby_form_definition(definition, slug, job_id)
    if not fields:
        raise CaptureShapeError(
            f"ashby: the form for {slug}/{job_id} yielded zero visible fields")
    posting_id = str(posting.get("id") or job_id)
    return FieldMap(vendor="ashby", posting_id=posting_id,
                    captured_at=_now(now), fields=fields)


def _parse_ashby_form_render(form: dict, slug: str, job_id: str) -> list[Field]:
    """Parse the live `FormRender` shape: `sections[].fieldEntries[]`.

    `isRequired`/`isHidden` live on the entry, not the nested `field`; a
    deactivated field is dropped alongside an explicitly hidden entry. A
    hidden section is skipped whole and does not consume a `step_index` slot
    (only visible sections count as steps).
    """
    sections = form.get("sections")
    if not isinstance(sections, list):
        raise CaptureShapeError(
            f"ashby: applicationForm.sections missing or not a list "
            f"for {slug}/{job_id}")
    fields: list[Field] = []
    step_index = 0
    for section in sections:
        if section.get("isHidden"):
            continue
        for entry in section.get("fieldEntries") or []:
            field_def = entry.get("field")
            if not isinstance(field_def, dict):
                raise CaptureShapeError(
                    f"ashby: a form entry in section "
                    f"{section.get('title')!r} for {slug}/{job_id} has no "
                    "nested 'field' object")
            if entry.get("isHidden") or field_def.get("isDeactivated"):
                continue
            title = (field_def.get("title") or field_def.get("humanReadablePath")
                     or "")
            fields.append(_build_ashby_field(
                key=field_def.get("path", ""),
                title=title,
                raw_type=field_def.get("type"),
                required=bool(entry.get("isRequired", False)),
                options=_ashby_options(field_def),
                step_index=step_index,
            ))
        step_index += 1
    return fields


def _parse_ashby_form_definition(definition: dict, slug: str, job_id: str) -> list[Field]:
    """Fallback for postings still served the pre-migration
    `applicationFormDefinition` shape (one-release grace period; drop this
    once no live posting exercises it). `isRequired`/`isHidden` live on the
    nested `field` itself here, and the shape carries no step concept, so
    every field stays `step_index=0` as before."""
    sections = definition.get("sections")
    if not isinstance(sections, list):
        raise CaptureShapeError(
            f"ashby: applicationFormDefinition.sections missing or not a list "
            f"for {slug}/{job_id}")
    fields: list[Field] = []
    for section in sections:
        for entry in section.get("fields") or []:
            field_def = entry.get("field")
            if not isinstance(field_def, dict):
                raise CaptureShapeError(
                    f"ashby: a form entry in section "
                    f"{section.get('title')!r} for {slug}/{job_id} has no "
                    "nested 'field' object")
            if field_def.get("isHidden"):
                continue
            fields.append(_build_ashby_field(
                key=field_def.get("path", ""),
                title=field_def.get("title", ""),
                raw_type=field_def.get("type"),
                required=bool(field_def.get("isRequired", False)),
                options=_ashby_options(field_def),
                step_index=0,
            ))
    return fields


def _build_ashby_field(*, key: str, title: str, raw_type, required: bool,
                       options: list[str], step_index: int) -> Field:
    field_type, role = _ashby_field_type(raw_type)
    return Field(
        key=key,
        label=title,
        type=field_type,
        required=required,
        options=options,
        source=ASHBY_SOURCE,
        locator=Locator(role=role, name=title),
        step_index=step_index,
        conditional_on=None,
    )


def _ashby_field_type(raw_type) -> tuple[str, str]:
    """Canonical (type, role) for a raw Ashby field `type` string.

    Known types route through the pinned map and fieldmap's shared
    `_role_for_type`, so browser and HTTP captures never drift on role naming.
    An unrecognised type is not fatal (Ashby adds field types over time): it
    is passed through lowercased with role "textbox" rather than raising.
    """
    canonical = _ASHBY_TYPE_MAP.get(raw_type)
    if canonical is not None:
        return canonical, _role_for_type(canonical)
    return str(raw_type or "").lower(), "textbox"


def _ashby_options(field_def: dict) -> list[str]:
    """Enumerated option labels for a select-type field.

    `selectableValues` is usually a direct sibling of `type` on the field, but
    some field shapes carry it nested under `metadata` instead; both
    locations are checked defensively.
    """
    values = field_def.get("selectableValues")
    if not isinstance(values, list):
        metadata = field_def.get("metadata")
        values = metadata.get("selectableValues") if isinstance(metadata, dict) else None
    if not isinstance(values, list):
        return []
    labels: list[str] = []
    for value in values:
        if isinstance(value, dict):
            labels.append(str(value.get("label", value.get("value", ""))))
        else:
            labels.append(str(value))
    return labels


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


# -- minimal HTML tree (stdlib html.parser; engine core stays dependency-free) -

class _Node:
    __slots__ = ("tag", "attrs", "children", "text_parts")

    def __init__(self, tag: str, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[_Node] = []
        self.text_parts: list[str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", [])
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self._stack[-1].text_parts.append(data)


def _build_tree(html_source: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html_source or "")
    builder.close()
    return builder.root


def _find_all(node: _Node, predicate) -> list[_Node]:
    out: list[_Node] = []
    for child in node.children:
        if predicate(child):
            out.append(child)
        out.extend(_find_all(child, predicate))
    return out


def _first(node: _Node, predicate):
    for child in node.children:
        if predicate(child):
            return child
        found = _first(child, predicate)
        if found is not None:
            return found
    return None


def _has_class(node: _Node, cls: str) -> bool:
    return cls in (node.attrs.get("class") or "").split()


def _node_text(node: _Node, exclude_cls: str | None = None) -> str:
    if exclude_cls and _has_class(node, exclude_cls):
        return ""
    parts = list(node.text_parts)
    for child in node.children:
        parts.append(_node_text(child, exclude_cls))
    return " ".join(part.strip() for part in parts if part and part.strip())


# -- shared helpers ------------------------------------------------------------

def _response_url(response) -> str:
    return getattr(response, "url", "") or ""


def _dig(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _now(now: Callable[[], str] | None) -> str:
    return (now or _utc_now_iso)()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
