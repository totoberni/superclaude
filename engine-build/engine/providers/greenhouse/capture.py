"""Greenhouse schema capture + parse (W5.1 Stage 2a; moved from engine.fieldmap).

Greenhouse is the browserless vendor: the sanctioned schema source is
`boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true`, one polite
GET per posting. `capture_greenhouse` fetches it; `parse_greenhouse` maps the
payload onto the canonical `FieldMap` (no I/O).

Only the GREENHOUSE-specific capture/parse code moved here. The generic helpers
that Workable's own capture (`capture_workable` / `parse_workable`) also relies
on -- `normalize_type` + `_HIDDEN_TYPES` (the shared vendor-native type mapper)
and `_read_body_text` -- STAY in `engine.fieldmap` and are imported from there;
the generic timestamp `_utc_now_iso` is single-sourced in
`engine.kernel.capture_toolkit` and imported from the kernel. Each has exactly
one definition. `capture_greenhouse` /
`parse_greenhouse` now live ONLY here: their callers import them from this
module directly (`greenhouse.fill.capture`'s call-time import, and the tests'
`from engine.providers.greenhouse.capture import ...`). The registry looks
`capture_greenhouse` up lazily as a CALL-TIME callable
(`PROVIDERS["greenhouse"].capture`), resolving the attribute on this module when
invoked, so the `monkeypatch.setattr(capture, "capture_greenhouse", ...)` seam
on this module still works. (`engine.fieldmap`'s lazy re-export shim carried the
old import paths until it was dissolved in Stage 5.)
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable

from engine.kernel.capture_toolkit import UA
from engine.fieldmap import (
    _HIDDEN_TYPES,
    _read_body_text,
    normalize_type,
)
from engine.kernel.capture_toolkit import _utc_now_iso
from engine.kernel.contracts import (
    Field,
    FieldMap,
    Locator,
    Section,
    _role_for_type,
)
from engine.kernel.resolve import _DECLINE_SECTIONS

# parse_greenhouse's `source` tag (the bucket a question arrived in) -> the
# canonical Section. Unrecognised sources fall back to STANDARD.
_SECTION_FOR_SOURCE = {
    "questions": Section.STANDARD,
    "location_questions": Section.LOCATION,
    "compliance": Section.COMPLIANCE_EEOC,
    "demographic": Section.DEMOGRAPHIC,
}

# GREENHOUSE-LOCAL role overrides on top of the kernel's generic `_role_for_type`
# (the same idiom workable's capture uses for its own DOM's roles). The kernel map
# is written for the vendors that DO serve the role it names; greenhouse does not,
# and the locator is a claim about THIS vendor's live DOM, so the correction
# belongs here rather than in the shared map (which the other vendors depend on).
#
# `multi_value_multi_select` -> CHECKBOX, not the kernel's `listbox`. LIVE
# (read-only fetch of a gitlab language-fluency question, 2026-07-14): greenhouse
# renders this control as a `<fieldset class="checkbox" aria-required="true">`
# holding SEVEN native `<input type="checkbox">`, one per option, with its question
# text in the `<legend>`. The page carries ZERO `role=listbox` nodes. A `listbox`
# locator therefore resolved to NOTHING, and the fill drove it anyway
# (`_NATIVE_SELECT_TYPES` -> `select_option`), burning a 30-second Playwright
# actionability timeout and booking the field as a `fill-error` -- on a control
# that is REQUIRED on every posting that serves it (55 live, across four boards).
# A role no element on the page carries is a CAPTURE BUG, so capture tells the
# truth: this is a group of checkboxes.
#
# The role is load-bearing beyond the locator: `checkbox` is a
# `fill_toolkit._CLICK_HAZARD_ROLES` member, so `fill()`'s `_needs_human_handoff`
# gate hands the control to a human BEFORE any locator is built (an anti-bot
# trusted-click hazard, exactly as for lever/ashby/workable). DRIVING the group
# (clicking each option checkbox by its OWN label, which does resolve 1-to-1) is
# W5.1c's cross-vendor job, not this wave's: until then the honest report is a
# NAMED hand-off, never a 30-second timeout dressed up as a fill error.
_ROLE_OVERRIDES = {"multi_value_multi_select": "checkbox"}

# The async education section (School / Degree / Discipline). Greenhouse does NOT
# serve these in the `questions=true` payload -- the payload carries only a
# top-level `education` TOGGLE (`education_optional` / `education_required` /
# `education_hidden`), and the section itself is rendered CLIENT-SIDE as
# debounced-remote-search react-select typeaheads when the toggle is enabled
# (confirmed on the canonical anthropic posting: `"education": "education_
# optional"`). Capture models the SCHEMA, so the STRUCTURAL signal it keys on is
# that toggle, NOT any label text: when enabled, emit the three typeahead fields
# the section always renders, tagged `EDUCATION_TYPEAHEAD_TYPE` so
# `greenhouse.fill._is_education_typeahead` routes them to the async-aware driver
# (the live 2026-07-18 run left them BLANK precisely because they were never
# captured, so never driven). The exact live react-select id per control is a
# TB5-R2 confirmation -- the keys below are stable namespaced identifiers (so
# they never collide with a schema question field literally named "degree"), and
# the fill driver uses the key as the react-select `field_id`; if the live DOM's
# id root differs, the key is where that mapping is pinned.
EDUCATION_TYPEAHEAD_TYPE = "education_typeahead"
_EDUCATION_ENABLED = frozenset({"education_optional", "education_required"})
_EDUCATION_TYPEAHEAD_CONTROLS = (
    ("education_school", "School"),
    ("education_degree", "Degree"),
    ("education_discipline", "Discipline"),
)


def _greenhouse_role_for_type(field_type: str) -> str:
    """The ARIA role greenhouse's LIVE DOM actually carries for this field type:
    the vendor-local override where the kernel's generic map is wrong for THIS
    vendor (see `_ROLE_OVERRIDES`), else the kernel's own answer."""
    return _ROLE_OVERRIDES.get(field_type) or _role_for_type(field_type)


def greenhouse_questions_url(slug: str, job_id: str) -> str:
    """The sanctioned schema endpoint (R-WT-8 D3): one GET, questions=true."""
    return ("https://boards-api.greenhouse.io/v1/boards/"
            f"{slug}/jobs/{job_id}?questions=true")


def greenhouse_apply_url(slug: str, job_id: str) -> str:
    """The public Greenhouse apply page (the `job-boards.greenhouse.io/{slug}/
    jobs/{job_id}` host is the newer variant of the same page)."""
    return f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"


def capture_greenhouse(slug: str, job_id: str, opener=None, *,
                       timeout_s: float = 20, user_agent: str = UA,
                       now: Callable[[], str] | None = None) -> FieldMap:
    """Capture one Greenhouse posting's field map via the questions endpoint.

    `opener` is any object exposing `.open(request, timeout=...)` (a urllib
    opener in production, a fake in tests); a single polite GET with the honest
    fetch-layer User-Agent is all the read costs.

    The response is the standard Greenhouse job payload carrying FOUR buckets,
    and they do NOT share one shape (assuming they did is what silently dropped
    the whole EEOC section until 2026-07-14):
      - `questions` / `location_questions`: FLAT lists of questions, each with
        its own `fields` array.
      - `compliance`: a list of BLOCKS, whose questions nest ONE LEVEL DEEPER
        (`_compliance_questions` flattens them; see its docstring).
      - `demographic_questions`: a separate object with its own question shape
        (`_fields_from_demographic`).
    Every QUESTION in all four becomes a Field, tagged by the section it came
    from. The EEOC/demographic ones are captured `required=False,
    decline_allowed=True` (`_DECLINE_SECTIONS`): owner policy DECLINES them, and
    declining is not the same as being blind to them -- they must be captured so
    the completeness census can account for them as justified skips.
    """
    opener = opener or urllib.request.build_opener()
    url = greenhouse_questions_url(slug, job_id)
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/json",
    })
    response = opener.open(request, timeout=timeout_s)
    raw = json.loads(_read_body_text(response))
    return parse_greenhouse(raw, slug, job_id, now=now)


def parse_greenhouse(raw: dict, slug: str, job_id: str, *,
                     now: Callable[[], str] | None = None) -> FieldMap:
    """Map a questions=true payload onto the canonical FieldMap (no I/O)."""
    captured_at = (now or _utc_now_iso)()
    posting_id = str(raw.get("id") or job_id)
    fields: list[Field] = []
    for section in ("questions", "location_questions"):
        for question in raw.get(section) or []:
            fields.extend(_fields_from_question(question, section))
    # `compliance` is NOT a flat question list (see `_compliance_questions`).
    for question in _compliance_questions(raw.get("compliance")):
        fields.extend(_fields_from_question(question, "compliance"))
    fields.extend(_fields_from_demographic(raw.get("demographic_questions")))
    # The education section is NOT a `questions` bucket; it is driven by the
    # top-level `education` toggle and rendered client-side (see the block
    # comment on `EDUCATION_TYPEAHEAD_TYPE`).
    fields.extend(_education_typeahead_fields(raw.get("education")))
    return FieldMap(vendor="greenhouse", posting_id=posting_id,
                    captured_at=captured_at, fields=fields)


def _education_typeahead_fields(education_toggle) -> list[Field]:
    """The School / Degree / Discipline async typeaheads Greenhouse renders when
    the posting's top-level `education` toggle is enabled (`education_optional` /
    `education_required`), else an empty list (`education_hidden`, absent, or any
    other value contributes nothing). Keyed STRUCTURALLY on the toggle, not on
    label text. `required` follows the toggle (`education_required` -> required);
    STANDARD section (not a decline class), so the resolver treats them as
    answerable from the SSOT `education` data."""
    if education_toggle not in _EDUCATION_ENABLED:
        return []
    required = education_toggle == "education_required"
    return [Field(
        key=key, label=label, type=EDUCATION_TYPEAHEAD_TYPE,
        required=required, options=[], source="education",
        locator=Locator(role="combobox", name=label),
        step_index=0, conditional_on=None,
        norm_type=normalize_type(EDUCATION_TYPEAHEAD_TYPE),
        section=Section.STANDARD,
    ) for key, label in _EDUCATION_TYPEAHEAD_CONTROLS]


def _compliance_questions(blocks) -> list[dict]:
    """Every QUESTION in the `compliance` bucket, whatever nesting it arrives in.

    THE LIVE SHAPE (boards-api, verified 2026-07-14 against the live anthropic
    seal posting): `compliance` is a list of BLOCKS, not of questions. Each block
    carries a `type` ("eeoc"), a `description`, and its OWN `questions` list --
    one level DEEPER than the `questions`/`location_questions` buckets:

        "compliance": [{"type": "eeoc", "description": "...",
                        "questions": [{"required": false, "label": "Gender",
                                       "fields": [{"name": "gender", ...}]}]}]

    A block has NO `fields` key of its own, so handing one straight to
    `_fields_from_question` (which reads `question["fields"]`) yielded NOTHING:
    the entire COMPLIANCE_EEOC section was silently dropped. Live on the
    anthropic posting that was FOUR EEOC controls (disability_status,
    veteran_status, race, gender) captured as zero Fields, which left the whole
    `decline_allowed` path dead code and the completeness census four controls
    short. The suite never noticed because the only test covering compliance fed
    a FLAT shape the API has never served.

    Blocks whose `questions` list is EMPTY contribute nothing, which is correct:
    the live payload leads with a description-only OMB burden-statement block.

    A BARE question (an entry carrying `fields` directly, the shape the old test
    invented) is still accepted, so neither shape can be silently dropped again.

    THE COMPLIANCE SCHEMA IS NOT THE COMPLIANCE DOM, and deliberately so. The API
    serves a `race` question (8 options); the live apply page renders four EEOC
    comboboxes with ids `gender`, `hispanic_ethnicity`, `veteran_status`,
    `disability_status` -- no `race` control at all, and a `hispanic_ethnicity` the
    schema never mentions. Capture models the SCHEMA, not the DOM, so it emits
    `race` and misses `hispanic_ethnicity`, and that is left alone rather than
    reconciled: every field in this bucket is `decline_allowed` (owner policy
    DECLINES the whole EEO/demographic class), so it is dropped by BOTH the kernel
    resolver and the content overlay and NO locator is ever built for it. A phantom
    inside the DECLINED set cannot bite: the fill never reaches for it. Pinned by
    `test_no_eeo_or_demographic_field_is_ever_driven_at_either_layer`; were the
    decline ever lifted, `race` would become a phantom locator and
    `hispanic_ethnicity` a `dom_only` sweep gap, and THAT is when this must be
    reconciled against the DOM.
    """
    out: list[dict] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        nested = block.get("questions")
        if isinstance(nested, list):
            out.extend(q for q in nested if isinstance(q, dict))
        elif block.get("fields"):
            out.append(block)
    return out


def _fields_from_question(question: dict, source: str) -> list[Field]:
    label = question.get("label", "")
    required = bool(question.get("required", False))
    section = _SECTION_FOR_SOURCE.get(source, Section.STANDARD)
    decline_allowed = section in _DECLINE_SECTIONS
    out: list[Field] = []
    for sub in question.get("fields") or []:
        field_type = sub.get("type", "input_text")
        if field_type in _HIDDEN_TYPES:
            continue  # input_hidden: portal tracking, never a user field
        out.append(Field(
            key=sub.get("name", ""),
            label=label,
            type=field_type,
            required=False if decline_allowed else required,
            options=_option_labels(sub.get("values")),
            source=source,
            locator=Locator(role=_greenhouse_role_for_type(field_type),
                            name=label),
            step_index=0,
            conditional_on=None,
            decline_allowed=decline_allowed,
            norm_type=normalize_type(field_type),
            section=section,
        ))
    return out


def _fields_from_demographic(block) -> list[Field]:
    """The demographic block is a separate object with its own question shape
    (`answer_options`, type on the question). Captured but always manual-only,
    and (W5) always `decline_allowed=True, required=False` regardless of what
    the raw payload's own `required` flag says (R-WT-8 8: never auto-answered,
    never blocking)."""
    if not isinstance(block, dict):
        return []
    out: list[Field] = []
    for question in block.get("questions") or []:
        field_type = question.get("type", "multi_value_single_select")
        out.append(Field(
            key=f"demographic_{question.get('id', '')}",
            label=question.get("label", ""),
            type=field_type,
            required=False,
            options=_option_labels(question.get("answer_options")),
            source="demographic",
            locator=Locator(role=_greenhouse_role_for_type(field_type),
                            name=question.get("label", "")),
            step_index=0,
            conditional_on=None,
            decline_allowed=True,
            norm_type=normalize_type(field_type),
            section=Section.DEMOGRAPHIC,
        ))
    return out


def _option_labels(values) -> list[str]:
    if not isinstance(values, list):
        return []
    labels: list[str] = []
    for value in values:
        if isinstance(value, dict):
            labels.append(str(value.get("label", value.get("value", ""))))
        else:
            labels.append(str(value))
    return labels
