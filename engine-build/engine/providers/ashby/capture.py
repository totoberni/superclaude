"""Ashby schema capture + parse (W5.1 Stage 2c; moved from engine.browse).

Ashby is the graphql-intercept vendor: the posting page is a SPA that fetches
its typed application schema through its own `non-user-graphql` (ApiJobPosting)
call. `capture_ashby` loads the page once, intercepts that response, and maps
its field definitions onto the canonical `FieldMap` (`source="ashby_graphql"`).
The typed schema lives at `data.jobPosting.applicationForm` (a `FormRender`,
live-confirmed 2026-07-03); the older `data.jobPosting.applicationFormDefinition`
shape is probed second as a one-release fallback. Every parser fails LOUDLY:
a shape mismatch raises `CaptureShapeError` naming the exact selector/key that
missed, NEVER a silently empty FieldMap.

Only the ASHBY-specific capture/parse code moved here. Its transitive closure is
DISJOINT from Lever's DOM parse (which stays in `engine.browse`): the Ashby path
works on the intercepted graphql JSON, never the HTML tree, so it shares NONE of
browse.py's label/text/tree helpers. Everything it does reach is generic
browser/capture INFRA already single-sourced in the kernel -- the browser page
factory + timeout + response-url reader + `_dig` + `CaptureShapeError` + `_now`
from `engine.kernel.capture_toolkit`, and the `Field`/`FieldMap`/`Locator`
contracts + `_role_for_type` from `engine.kernel.contracts` -- so nothing is
re-implemented and there is exactly one home for each name.

`engine.browse` keeps a LAZY re-export shim (PEP 562 `__getattr__`) for every
name moved here, so existing importers keep resolving them via `engine.browse`
unchanged: `registry._capture_ashby` / `_apply_ashby`'s call-time
`browse.capture_ashby` / `browse.ashby_application_url`, `engine.fill`'s call-time
`from engine.browse import capture_ashby`, and the tests'
`from engine.browse import ASHBY_SOURCE, capture_ashby`, `browse._parse_ashby`,
and `monkeypatch.setattr(browse, "capture_ashby", ...)` seam.

LAZY-IMPORT INVARIANT (mirrors browse.py / base.py / registry.py): patchright is
imported lazily inside `_default_browser_page` (in the kernel), only when a real
capture runs, so importing this module -- and importing `engine.providers.ashby`
-- stays browser-free for the daily poller. Tests drive the capture path with a
fake browser/page factory and never touch patchright or the network.
"""

from __future__ import annotations

from typing import Callable

from engine.kernel.capture_toolkit import (
    _TIMEOUT_MS,
    CaptureShapeError,
    _default_browser_page,
    _dig,
    _now,
    _response_url,
)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    Locator,
    _role_for_type,
)

ASHBY_SOURCE = "ashby_graphql"

# The SPA response we intercept: the substring that marks Ashby's own typed-form
# graphql call among every response the posting page fires.
_ASHBY_GRAPHQL_MARKER = "non-user-graphql"

# Ashby ApiJobPosting field `type` -> canonical FieldMap type. The a11y role is
# then derived from the canonical type via the kernel's single source of truth
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


def ashby_application_url(slug: str, job_id: str) -> str:
    return f"https://jobs.ashbyhq.com/{slug}/{job_id}/application"


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

    Known types route through the pinned map and the kernel's shared
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
