"""ATS browser field-map capture via headless Playwright (W4 3.2).

Reaches the two vendors that a browserless HTTP probe cannot enumerate:

- Ashby: the posting page is a SPA that fetches its typed application schema
  through its own `non-user-graphql` (ApiJobPosting) call. We load the page,
  intercept that response, and map its field definitions onto the canonical
  FieldMap. Ashby forms carry NO conditional visibility (R-WT-8 8), so the map
  is static and every field is `step_index=0`.
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

CAVEAT (load-bearing): the graphql envelope keys and the Lever DOM selectors
encoded here are FIXTURE-DERIVED and plausible, not yet confirmed against live
pages. They will be validated LIVE on toto during the second SSOT playtest loop
(meta-driven). Until then, every parser MUST fail LOUDLY and descriptively:
a shape mismatch raises `CaptureShapeError` naming the exact selector/key that
missed, NEVER a silently empty FieldMap. Callers stay fail-soft (run.py counts a
failed capture and moves on) so a loud parser and a resilient runner coexist.
"""

from __future__ import annotations

import contextlib
import re
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
    """Pick the intercepted graphql response carrying the ApiJobPosting form."""
    for response in responses:
        try:
            body = response.json()
        except Exception:
            continue
        posting = _dig(body, "data", "jobPosting")
        if isinstance(posting, dict) and posting.get("applicationFormDefinition"):
            return posting
    raise CaptureShapeError(
        f"ashby: no {_ASHBY_GRAPHQL_MARKER!r} response for {slug}/{job_id} "
        "carried data.jobPosting.applicationFormDefinition "
        f"(saw {len(responses)} matching-URL response(s)); the graphql shape has "
        "drifted or the form never loaded")


def _parse_ashby(posting: dict, slug: str, job_id: str, *,
                 now: Callable[[], str] | None = None) -> FieldMap:
    definition = posting.get("applicationFormDefinition") or {}
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
            fields.append(_ashby_field(field_def))
    if not fields:
        raise CaptureShapeError(
            f"ashby: the form for {slug}/{job_id} yielded zero visible fields")
    posting_id = str(posting.get("id") or job_id)
    return FieldMap(vendor="ashby", posting_id=posting_id,
                    captured_at=_now(now), fields=fields)


def _ashby_field(field_def: dict) -> Field:
    field_type = _ASHBY_TYPE_MAP.get(field_def.get("type"), "input_text")
    title = field_def.get("title", "")
    return Field(
        key=field_def.get("path", ""),
        label=title,
        type=field_type,
        required=bool(field_def.get("isRequired", False)),
        options=_ashby_options(field_def.get("selectableValues")),
        source=ASHBY_SOURCE,
        locator=Locator(role=_role_for_type(field_type), name=title),
        step_index=0,
        conditional_on=None,
    )


def _ashby_options(values) -> list[str]:
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
    fields = _lever_base_fields(tree) + _lever_custom_fields(tree, slug, job_id)
    if not fields:
        raise CaptureShapeError(
            f"lever: the apply page for {slug}/{job_id} rendered no recognizable "
            "form fields (no .application-field or .application-question blocks "
            "found); the DOM shape has drifted or the page did not load")
    return FieldMap(vendor="lever", posting_id=str(job_id),
                    captured_at=_now(now), fields=fields)


def _lever_base_fields(tree: "_Node") -> list[Field]:
    """The fixed base fields: every input inside an `.application-field` block."""
    fields: list[Field] = []
    for container in _find_all(tree, lambda n: _has_class(n, "application-field")):
        control = _first(container, _is_form_control)
        if control is None:
            continue
        name = control.attrs.get("name", "")
        if not name:
            continue
        label = _control_label(container) or name
        field_type = _control_type(control)
        fields.append(Field(
            key=name,
            label=label,
            type=field_type,
            required=_is_required(container, control),
            options=_select_options(control),
            source=LEVER_SOURCE,
            locator=Locator(role=_role_for_type(field_type), name=label),
            step_index=0,
            conditional_on=None,
        ))
    return fields


def _lever_custom_fields(tree: "_Node", slug: str, job_id: str) -> list[Field]:
    return [_lever_custom_field(card, slug, job_id)
            for card in _find_all(tree,
                                  lambda n: _has_class(n, "application-question"))]


def _lever_custom_field(card: "_Node", slug: str, job_id: str) -> Field:
    label = _control_label(card)
    controls = [n for n in _find_all(card, _is_form_control)
                if (n.attrs.get("type") or "").lower() != "hidden"]
    if not controls:
        raise CaptureShapeError(
            f"lever: custom question card {label!r} on {slug}/{job_id} has no "
            "input/select/textarea control")
    checkboxes = [n for n in controls
                  if (n.attrs.get("type") or "").lower() == "checkbox"]
    if len(checkboxes) > 1:
        field_type = "multi_value_multi_select"
        options = [_checkbox_label(cb) for cb in checkboxes]
        key = checkboxes[0].attrs.get("name", "") or _slug(label)
    else:
        primary = controls[0]
        field_type = _control_type(primary)
        options = _select_options(primary)
        key = primary.attrs.get("name", "") or _slug(label)
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
