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
DISJOINT from Lever's DOM parse (which lives in `engine.providers.lever.capture`):
the Ashby path works on the intercepted graphql JSON, never the HTML tree, so it
shares NONE of Lever's label/text/tree helpers. Everything it does reach is generic
browser/capture INFRA already single-sourced in the kernel -- the browser page
factory + timeout + response-url reader + `_dig` + `CaptureShapeError` + `_now`
from `engine.kernel.capture_toolkit`, and the `Field`/`FieldMap`/`Locator`
contracts + `_role_for_type` from `engine.kernel.contracts` -- so nothing is
re-implemented and there is exactly one home for each name.

The `engine.browse` re-export shim that once forwarded these names was dissolved
in Stage 4: every importer now reaches this module directly. `engine.fill._capture`
imports `capture_ashby` from here, and the tests import `ASHBY_SOURCE`,
`capture_ashby`, and `_parse_ashby` from `engine.providers.ashby.capture`; a test
that needs to swap `capture_ashby` monkeypatches it on this module.

LAZY-IMPORT INVARIANT (mirrors base.py): patchright is
imported lazily inside `_default_browser_page` (in the kernel), only when a real
capture runs, so importing this module -- and importing `engine.providers.ashby`
-- stays browser-free for the daily poller. Tests drive the capture path with a
fake browser/page factory and never touch patchright or the network.
"""

from __future__ import annotations

import re
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

# -- GEO-AUTOCOMPLETE LOCATION FIELD (W5B-ASHBY F1) ----------------------------
# POSTING REGISTRY. The evidence below was taken on elevenlabs postings that are
# named with their original attribution (that is what records where and when each
# claim was observed). elevenlabs/b43e388f -- the then-acceptance posting and the
# source of tests/fixtures/.../dom.html -- is CLOSED (verified gone 2026-07-14);
# it is history, never a live target. The CURRENT live target is
# axelera/17131e36-e361-45a7-8538-1033a63addd1 (fallback axelera/0505a9a5); see
# `fill.py` § POSTING REGISTRY for the full entry.
#
# The live-DOM root cause of the 2026-07-12 acceptance timeout ("press_
# sequentially Timeout 20000ms, waiting for textbox Location"), re-probed live
# 2026-07-13 on elevenlabs/b43e388f (CLOSED since):
#
#   graphql: {"path": "0a131ea7-...", "type": "Location", "title": "Location",
#             "required": true}
#   DOM:     <input class="_input_d7ago_28" placeholder="Start typing..."
#                   aria-autocomplete="list" aria-expanded="false"
#                   aria-haspopup="listbox" role="combobox">
#
# `Location` is in NO `_ASHBY_TYPE_MAP` entry, so it fell through the
# unknown-type branch of `_ashby_field_type` and was typed role "textbox"; the
# live control is a React geo-AUTOCOMPLETE whose role is "combobox", so
# `get_by_role("textbox", name="Location")` never resolved and Playwright timed
# out before a single keystroke. A location-shaped field therefore carries the
# COMBOBOX role here, which is what routes it to the geo driver
# (`fill._fill_geo_autocomplete`) instead of `type_human`.
#
# The role is a declaration of the CONTROL KIND, not a resolvable selector: the
# live input carries no id, no name and no aria-label (its `<label>` has no
# `for`), so it has NO accessible name and role+name cannot reach it either. The
# geo driver anchors on the field entry's own `data-field-path` attribute
# instead (see `fill._geo_control`); the schema `type` stays untouched so the
# kernel keeps rendering the field as free text from the SSOT.
#
# RATIFICATION MARKER (W5B-ASHBY): the task-spec words the trigger as "label
# normalized CONTAINS location or city". This implements it as a TOKEN match,
# not a substring match.
#
# The word that discriminates the two rules is the NOUN "relocation", which
# literally contains the substring "location" (re-LOCATION). The VERB "relocate"
# does NOT contain it, so an earlier draft of this marker justified the
# deviation with an example ("Are you willing to relocate?") that a substring
# rule handles correctly anyway; the claim was false and is corrected here.
#
# A relocation question ("Do you require relocation assistance?", "What is your
# relocation preference?": CONSTRUCTED examples, not observed labels, since the
# then-sealed elevenlabs/b43e388f posting -- CLOSED since 2026-07-14, see the
# POSTING REGISTRY above -- served no relocation question at all and its captured
# DOM carries zero "relocat" labels) is an ANSWERABLE field, not a
# place field: the kernel resolves ANY "relocat" label from canned answers
# (kernel/resolve.py `_ANSWER_MATCHERS`, the ("relocat",) row). Under a
# SUBSTRING rule such a field captures role "combobox", routes to
# `fill._fill_geo_autocomplete`, types its canned answer as a place filter,
# matches no place suggestion, and lands honestly unfilled: a required, fillable
# field silently lost, which is the harm this wave exists to prevent.
#
# The kernel's own value-side matcher is a substring matcher and hits exactly
# this collision: its generic location row carries the bare substring "location"
# and would claim a "relocation" label too, and only ROW ORDER (the "relocat"
# row precedes it) keeps it off one. A single boolean predicate has no ordering
# escape, so it discriminates by TOKEN instead. Token matching is the narrower,
# safer reading of the same intent, and the relocation trap in
# `test_ashby_location_captured_as_combobox` pins it: under a substring rule
# that field captures as a combobox and the test fails.
_GEO_LABEL_TOKENS = frozenset({"location", "city"})
_GEO_ROLE = "combobox"
# The canonical types a geo-autocomplete may wear: an explicit String field, or
# any type the map does not know (today: the live `Location`). A select-typed
# field (a "Preferred office location" ValueSelect with real options) is NOT a
# geo autocomplete: it keeps its options and its controlled-component driver.
_GEO_CANDIDATE_TYPES = frozenset({"input_text"})
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# == THE ROLE INVARIANT (W5B-ASHBY round 7) ===================================
#
#   A control is captured with the role the LIVE DOM gives it, never a role
#   inferred from its schema type.
#
# Stated ONCE, here, and enforced at ONE site (`_rendered_roles` -> the `dom_role`
# argument of `_build_ashby_field`). Everything below is the evidence for why the
# type CANNOT decide the role on this vendor, and what breaks when it tries.
#
# Ashby's graphql `type` does NOT determine the control. The SAME `ValueSelect`
# renders as a native radio group on 33 of 34 live fields and as a nameless React
# dropdown on the 34th; the schema carries no display hint whatsoever. So the
# live DOM is the only signal, and a role derived from the type is a GUESS that
# is wrong for three of Ashby's ten types. A wrong role is not cosmetic: it is a
# latent FILL ERROR. `base._locate` builds `page.get_by_role(role, name=label)`,
# a role no element carries resolves to ZERO elements, the fill raises, and the
# field dies as a `fill-error:` in required_unfilled -- a CAPTURE BUG and a seal
# REJECT, never a subtractable click-debt.
#
# LIVE EVIDENCE (30 elevenlabs postings, re-probed 2026-07-13 with a real
# browser; the raw type on the left, the control Chrome actually renders on the
# right, and the role the KERNEL's generic `_role_for_type` would have guessed):
#
#   raw type          live control              live ARIA role   kernel guess
#   ---------------------------------------------------------------------------
#   String       x98  input[type=text]          textbox          textbox     ok
#   LongText     x98  textarea                  textbox          textbox     ok
#   Email        x30  input[type=email]         textbox          textbox     ok
#   File         x30  input[type=file]          button           button      ok
#   Location     x30  input[role=combobox]      combobox         textbox     WRONG
#   Boolean       x4  input[type=checkbox]      checkbox         checkbox    ok
#   ValueSelect  x33  input[type=radio] xN      radio            combobox    WRONG
#   ValueSelect   x1  input[role=combobox]      combobox         combobox    ok
#   MultiValueSel x6  input[type=checkbox] xN   checkbox         listbox     WRONG
#   Number        x1  input[type=number]        spinbutton       textbox     WRONG
#
# Three of those four WRONG rows were live PHANTOMS at round 6 (roles that ZERO
# elements on the page carry): `listbox` for a MultiValueSelect, `textbox` for a
# Number, and `textbox` for a Location. The fourth (a radio-rendered ValueSelect
# captured `combobox`) is the 2026-07-12 acceptance timeout this wave opened on.
# Every one of them is the SAME defect, and the fix is to stop guessing: the
# entry's own control decides.
#
# TWO ROLES ARE CLICK HAZARDS (`kernel.fill_toolkit._CLICK_HAZARD_ROLES` =
# {checkbox, radio}) and are probed FIRST, so an entry holding any tickable
# control fails SAFE: the fill hands it to a human rather than typing into it.
# That is the honest state until the W5.1c click-policy wave teaches the engine a
# trusted click; this wave does NOT click (that touches the frozen kernel policy),
# it makes the capture correct so the control is a NAMED, subtractable hand-off
# instead of a fill-error reject.
#
# The DOM is consulted ONLY for the control's SHAPE, never scraped for content:
# option labels always come from the graphql `selectableValues`. (They have to:
# Ashby's radios carry no `value` attribute at all, and its checkboxes carry the
# option wording in `name`, so the DOM is a poor option oracle and the schema is
# an exact one.)
#
# Ordered CSS probes; the FIRST match inside the entry decides. Every shape here
# was OBSERVED live on the postings above -- an entry whose control matches none
# of them yields NO dom role, and the field falls back to `_ashby_field_type`
# rather than inventing a claim about DOM we have never seen.
_DOM_ROLE_PROBES = (
    ('input[type="radio"]', "radio"),          # CLICK HAZARD: probed first
    ('input[type="checkbox"]', "checkbox"),    # CLICK HAZARD: probed first
    ('input[role="combobox"]', "combobox"),    # Ashby's nameless React widget
    ('input[type="number"]', "spinbutton"),
    ('input[type="file"]', "button"),
    ('select[multiple]', "listbox"),
    ('select', "combobox"),
    ('textarea', "textbox"),
    ('input[type="text"]', "textbox"),
    ('input[type="email"]', "textbox"),
    ('input[type="tel"]', "textbox"),
    ('input[type="url"]', "textbox"),
    ('input[type="date"]', "textbox"),
)

# The role fallback, reached ONLY when the live DOM cannot answer for a field:
# its entry is not MOUNTED (a field on a not-yet-reached step), or the capture
# ran with no page at all. It encodes what Ashby's live DOM has been OBSERVED to
# render for that raw type, NOT the kernel's generic type->role guess, because
# two of those guesses are phantoms on EVERY Ashby page (see the table above).
# The kernel map is generic across vendors and stays frozen and correct for them;
# this is Ashby's vendor-local correction to it, and it exists so that an
# unmounted field is not captured with a role its control could never have.
_ASHBY_FALLBACK_ROLE = {
    "MultiValueSelect": "checkbox",   # a native checkbox group (6/6 live)
    "Number": "spinbutton",           # an input[type=number] is not a textbox
}


def _is_geo_label(title: str) -> bool:
    """True iff the field's label reads as a place field ("Location", "City",
    "Current location (city, country)"), by TOKEN (never substring; see the
    ratification marker above)."""
    tokens = {t for t in _TOKEN_SPLIT_RE.split(str(title or "").lower()) if t}
    return bool(tokens & _GEO_LABEL_TOKENS)


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
        # DOM probe (page still open): the role the LIVE control carries, which
        # is the ONLY signal for what each field actually is (the role invariant).
        dom_roles = _rendered_roles(page)
    return _parse_ashby(posting, slug, job_id, dom_roles=dom_roles, now=now)


def _rendered_roles(page) -> dict:
    """The ARIA role the LIVE DOM gives each mounted field entry's control,
    keyed by `data-field-path` (== the graphql `path` == `Field.key`).

    This is the single enforcement site of the role invariant above. An entry the
    page does not carry (an off-step field) is simply absent from the map, and an
    entry whose control matches no probe contributes nothing, so the fallback
    decides those and only those.

    Fully guarded: a page or fixture that cannot answer the locator query yields
    the empty map, so the capture never CRASHES for want of a DOM and an
    HTTP-only path degrades to the fallback role for every field."""
    roles: dict[str, str] = {}
    for entry in _locate_all(page, "[data-field-path]"):
        path = _attr(entry, "data-field-path")
        if not path:
            continue
        role = _entry_role(entry)
        if role:
            roles[path] = role
    return roles


def _entry_role(entry) -> str:
    """The ARIA role of ONE entry's control, by ordered CSS probe (hazard first).
    Empty when the entry holds no control shape this vendor has been seen to
    render, which hands the decision to the fallback rather than guessing."""
    for css, role in _DOM_ROLE_PROBES:
        if _locate_all(entry, css):
            return role
    return ""


def _locate_all(scope, css: str) -> list:
    """`scope.locator(css).all()`, guarded end to end (mirrors the fill module's
    own DOM-read helper): a scope missing `.locator`, or a locator that raises,
    is treated as zero matches rather than crashing the capture."""
    locator_fn = getattr(scope, "locator", None)
    if not callable(locator_fn):
        return []
    try:
        return list(locator_fn(css).all() or [])
    except Exception:
        return []


def _attr(node, name: str) -> str:
    getter = getattr(node, "get_attribute", None)
    if not callable(getter):
        return ""
    try:
        return (getter(name) or "").strip()
    except Exception:
        return ""


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
                 dom_roles: dict | None = None,
                 now: Callable[[], str] | None = None) -> FieldMap:
    dom_roles = dom_roles or {}
    form = posting.get("applicationForm")
    if isinstance(form, dict) and form:
        fields = _parse_ashby_form_render(form, slug, job_id, dom_roles)
    else:
        definition = posting.get("applicationFormDefinition")
        definition = definition if isinstance(definition, dict) else {}
        fields = _parse_ashby_form_definition(definition, slug, job_id, dom_roles)
    if not fields:
        raise CaptureShapeError(
            f"ashby: the form for {slug}/{job_id} yielded zero visible fields")
    posting_id = str(posting.get("id") or job_id)
    return FieldMap(vendor="ashby", posting_id=posting_id,
                    captured_at=_now(now), fields=fields)


def _parse_ashby_form_render(form: dict, slug: str, job_id: str,
                             dom_roles: dict | None = None) -> list[Field]:
    """Parse the live `FormRender` shape: `sections[].fieldEntries[]`.

    `isRequired`/`isHidden` live on the entry, not the nested `field`; a
    deactivated field is dropped alongside an explicitly hidden entry. A
    hidden section is skipped whole and does not consume a `step_index` slot
    (only visible sections count as steps).
    """
    dom_roles = dom_roles or {}
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
            path = field_def.get("path", "")
            fields.append(_build_ashby_field(
                key=path,
                title=title,
                raw_type=field_def.get("type"),
                required=bool(entry.get("isRequired", False)),
                options=_ashby_options(field_def),
                step_index=step_index,
                dom_role=dom_roles.get(path, ""),
            ))
        step_index += 1
    return fields


def _parse_ashby_form_definition(definition: dict, slug: str, job_id: str,
                                 dom_roles: dict | None = None) -> list[Field]:
    """Fallback for postings still served the pre-migration
    `applicationFormDefinition` shape (one-release grace period; drop this
    once no live posting exercises it). `isRequired`/`isHidden` live on the
    nested `field` itself here, and the shape carries no step concept, so
    every field stays `step_index=0` as before.

    It threads `dom_roles` exactly as the modern parser does: the role invariant
    is a property of the LIVE PAGE, not of which graphql shape that page happened
    to be served, and a posting still on the legacy shape renders the same
    controls."""
    dom_roles = dom_roles or {}
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
            path = field_def.get("path", "")
            fields.append(_build_ashby_field(
                key=path,
                title=field_def.get("title", ""),
                raw_type=field_def.get("type"),
                required=bool(field_def.get("isRequired", False)),
                options=_ashby_options(field_def),
                step_index=0,
                dom_role=dom_roles.get(path, ""),
            ))
    return fields


def _build_ashby_field(*, key: str, title: str, raw_type, required: bool,
                       options: list[str], step_index: int,
                       dom_role: str = "") -> Field:
    field_type, role = _ashby_field_type(raw_type, title)
    # THE ROLE INVARIANT, enforced (see the section comment above): the LIVE DOM
    # decides. `_ashby_field_type`'s role is only the FALLBACK for a field whose
    # entry the page did not carry.
    #
    # Only the ROLE changes. The TYPE always stays what the schema said (a
    # radio-rendered ValueSelect IS still a single-select, and the resolver still
    # answers it from the same options), the QUESTION stays the label, and the
    # option LABELS stay the options -- so every value-side consumer (the
    # kernel's classifier, renderer and coverage report) is untouched by what the
    # DOM turned out to be.
    if dom_role:
        role = dom_role
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


def _ashby_field_type(raw_type, title: str = "") -> tuple[str, str]:
    """Canonical type, and the FALLBACK role, for a raw Ashby field `type`.

    The TYPE half is authoritative and always used: known types route through the
    pinned map; an unrecognised type is not fatal (Ashby adds field types over
    time) and is passed through lowercased rather than raising.

    The ROLE half is a FALLBACK ONLY. It is overridden by the live DOM whenever
    the field's entry is mounted (`_build_ashby_field`), and is reached only for a
    field the page did not carry. It is built in three layers:

      1. `_ASHBY_FALLBACK_ROLE`, Ashby's vendor-local correction to the two roles
         the generic kernel map gets wrong on EVERY Ashby page (a MultiValueSelect
         is a checkbox group, not a listbox; a Number is a spinbutton, not a
         textbox). Without this layer an off-step field of either type would be
         captured with a role no Ashby control can have.
      2. the kernel's shared `_role_for_type` for every other known type, so
         browser and HTTP captures never drift on role naming.
      3. role "textbox" for an unknown type.

    ONE label-driven override survives (W5B-ASHBY F1): a location-shaped label on
    a String/unknown type is Ashby's geo-AUTOCOMPLETE, a React combobox, not a
    text box. It is the one case where the SCHEMA cannot be right and the label is
    the only offline signal we have; live it is redundant (the DOM says combobox
    for all 30 of them) and it exists so an UNMOUNTED Location field is not
    captured as a textbox that resolves to nothing. A select-typed field never
    enters this branch: it has real options and is never a place field.
    """
    canonical = _ASHBY_TYPE_MAP.get(raw_type)
    known = canonical is not None
    if not known:
        canonical = str(raw_type or "").lower()
    role = _ASHBY_FALLBACK_ROLE.get(raw_type) or (
        _role_for_type(canonical) if known else "textbox")
    if (role == "textbox" and _is_geo_label(title)
            and (not known or canonical in _GEO_CANDIDATE_TYPES)):
        return canonical, _GEO_ROLE
    return canonical, role


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
