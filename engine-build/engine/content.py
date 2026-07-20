"""SSOT-grounded content channel: canned routing, option matching, essay delivery.

Sibling of `engine.draft`: it lives OUTSIDE `engine/kernel/` (the kernel is frozen)
and OUTSIDE `engine/providers/<vendor>/` (all four vendor plugins may import it
without breaching their pairwise import-disjointness).

Two jobs, both DETERMINISTIC and OFFLINE:

1. ROUTE the canned answers the kernel resolver leaves behind. The kernel's
   `_ANSWER_MATCHERS` is hash-adjacent frozen surface; this module carries its OWN
   `_CONTENT_MATCHERS` table for the question families the kernel never routed
   (in-office attendance, previously applied / interviewed, the verbose
   relocation dropdown), and per-application POLICY resolvers for the families
   whose answer depends on the posting (start date, city, referral, and the
   deterministic "how did you hear" option mapper).
2. DELIVER pre-generated free-text answers (essays, cover letters, any
   write-your-answer field) that `bin/generate_answers.py` produced OFFLINE, on
   toto, from the SSOT. Generation is a separate tool the engine NEVER imports.

STATUS: live. `w5_accept.py` wires `apply_content_overlay` into the fill path: it
calls `resolve_values`, then this overlay (which ADDS resolved values), then the
browser fill consumes the mutated values. The overlay's report is built and read
by that same acceptance harness.

Hard invariants (gate-enforced):

* NO network, NO subprocess, NO LLM call happens here. This module imports only
  the standard library, `yaml`, and the kernel TYPE/SSOT/POLICY modules. The two
  kernel helpers it reuses are `_is_upload_field` (`engine.kernel.fill_toolkit`)
  and `_DECLINE_SECTIONS` (`engine.kernel.resolve`), both stdlib + contracts
  only: the upload predicate and the never-auto-answer policy each have a single
  source of truth in the kernel and are deliberately NOT mirrored here.
* The overlay is ADDITIVE ONLY. It reads `ResolvedValues.skipped` and may promote
  an entry into `ResolvedValues.fields`; it NEVER modifies, re-renders, or
  replaces a value the kernel already resolved.
* The overlay NEVER overturns a policy skip. A COMPLIANCE_EEOC / DEMOGRAPHIC /
  VOLUNTARY field (or any field the vendor marked `decline_allowed`) is never
  auto-answered by the kernel, and is therefore never auto-answered here either
  (`is_policy_declined`) -- not from a canned route, and not from a generated
  answer. Overturning it would fill a gender/veteran/disability question the
  kernel declined ON PURPOSE, and would inflate completeness with it.
* A ToS-forbidden field is NEVER filled and is NEVER hidden: it is reported
  (`OverlayReport.tos_forbidden`) so that the acceptance gate can subtract it
  explicitly, once wired, instead of the engine quietly pretending the field does
  not exist.
* A canned ONE-WORD route never lands in an ESSAY-SHAPED field (`is_free_text`:
  no options, and either a long-text control or an essay-sized `max_length`). The
  canned table matches loose label substrings, and the same question ("are you
  willing to relocate?") is asked both as a two-option dropdown and as an
  800-character essay. Only the SHAPE tells them apart, so an essay field takes
  generated answers only: filling it with "Yes" would answer an essay with a
  word, discard the essay the generator wrote for that very field, and count the
  field complete. Canned routes therefore reach exactly the fields that carry
  OPTIONS or are short-text shaped.
* Nothing is guessed. A candidate value that cannot be mapped onto a unique
  option label stays skipped ("no option match"); a value longer than the field's
  own `max_length` stays skipped too (the form would truncate it mid-sentence);
  a field with no canned route and no generated answer stays skipped with its
  original reason.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from engine.kernel.contracts import Field, FieldMap, FieldValue, ResolvedValues
from engine.kernel.fill_toolkit import _is_upload_field
from engine.kernel.resolve import (
    _DECLINE_SECTIONS,
    _missing_path_guess,
    TOS_FORBIDDEN_SKIP_PREFIX,
)
from engine.kernel.ssot import MISSING, SSOT

CONTENT_VERSION = "1"

# The skip reason recorded when a candidate value exists but no unique option
# label carries it. Deliberately a single constant: the vendor loops and the
# acceptance gate both key on this exact string.
NO_OPTION_MATCH = "no option match"

# The skip reason recorded when a value is longer than the field's own
# `max_length`. Same posture as NO_OPTION_MATCH: a value the form would truncate
# mid-sentence is NOT filled, and the field stays skipped with a reason a human
# can act on. A single constant for the same reason: the loops key on the string.
OVER_MAX_LENGTH = "answer over the field's max length"

_WS_RE = re.compile(r"\s+")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# The yes/no vocabulary of the option matcher. Deliberately EN + IT ONLY: those
# are the two languages the owner applies in, so those are the only two in which
# a "Yes"/"Si" equivalence is a FACT rather than a guess ("si" also covers the
# accented "si" once `_leading_yes_no` folds the accent).
#
# Deliberately NOT a translation table, and deliberately NOT extended by adding
# languages the owner does not apply in. An English canned "Yes" is never equated
# with a French option "Oui, je suis disponible": that equivalence would be a
# GUESS about what the option means. An unmatched language therefore FAILS CLOSED
# to NO_OPTION_MATCH BY DESIGN -- a justified skip the gate can see and a human
# can finish, never a filled field nobody checked.
_YES_NO_TOKENS = {"yes", "no", "si"}

# The length cap at or above which a bare text input (no option list) is a
# WRITE-YOUR-ANSWER box rather than a one-line field. 300 characters is well above
# any one-line answer a form asks for ("City", "Notice period", a URL) and well
# below the essay boxes, which run 800 characters and up.
FREE_TEXT_MIN_LENGTH = 300


def is_long_text(field_type: str = "", norm_type: str = "") -> bool:
    """True iff the CONTROL is a long-text box: a `textarea`, a vendor `paragraph`
    control, or a field the capture normalized to LONGTEXT.

    Keyed on the control, never on the label. The same question is asked both as a
    two-option dropdown and as an 800-character essay box, and only the control
    tells the two apart -- which is exactly what keeps a one-word canned scalar
    out of the essay (see `apply_content_overlay` step 2).
    """
    lowered = str(field_type or "").lower()
    return ("textarea" in lowered or "paragraph" in lowered
            or str(norm_type or "").upper() == "LONGTEXT")


def is_free_text(field_type: str = "", norm_type: str = "",
                 max_length: int | None = None,
                 options: list[str] | None = None) -> bool:
    """True iff the field expects the applicant to WRITE an answer: it carries NO
    option list, and it is either a long-text control (`is_long_text`) or a bare
    text input with an essay-sized length cap (`FREE_TEXT_MIN_LENGTH`).

    Essay-SHAPED, not essay-LABELLED. A vendor that renders its 800-character
    "why us?" box as a plain `input_text` with `max_length: 800` and no
    `norm_type` is asking for an essay just as much as a `textarea` is, so the
    cap counts as much as the control does.

    The single definition BOTH ends of the content channel key on: the offline
    generator (`bin/generate_answers.py`) uses it to decide which questions go to
    a model, and the overlay uses it to decide which fields a canned scalar may
    never fill (step 2). Mirroring it in the generator would let the two drift,
    and a drift here means an essay the generator wrote is discarded by an overlay
    that no longer agrees the field is an essay.
    """
    if options:
        return False
    return (is_long_text(field_type, norm_type)
            or int(max_length or 0) >= FREE_TEXT_MIN_LENGTH)


def is_policy_declined(fld: Field) -> bool:
    """True iff policy forbids auto-answering this field: a COMPLIANCE_EEOC /
    DEMOGRAPHIC / VOLUNTARY section, or a vendor-declared `decline_allowed`.

    The single predicate BOTH ends of the content channel key on: the overlay
    (which must never overturn the kernel's deliberate skip) and the offline
    generator (which must never even send such a question to a model). The
    section set comes from the kernel resolver, so the policy has one definition;
    `decline_allowed` is the vendor's own tag for the same class of field.
    """
    return bool(fld.decline_allowed) or fld.section in _DECLINE_SECTIONS


class ContentSchemaError(ValueError):
    """A generated-answers YAML file is malformed or missing a mandatory key.

    Raised, never swallowed: a corrupt answers file must stop the loop loudly
    rather than silently degrade into "no answers", which would read as a
    justified skip and quietly lower completeness.
    """


# -- generated-answers schema (frozen, produced by bin/generate_answers.py) ----

@dataclass
class GeneratedAnswer:
    """One pre-generated free-text answer. At least one of key/label is set:
    `key` is the fieldmap key when the generator knew it, `label` is the posting's
    question text (the fallback join when a vendor re-keys its form between
    capture and fill)."""
    key: str | None
    label: str | None
    value: str


@dataclass
class TosForbidden:
    """A question the vendor's or employer's ToS forbids us to auto-answer.
    Carried explicitly (never dropped) so the gate subtracts it by name."""
    label: str
    reason: str


@dataclass
class DateControl:
    """What ONE date control on the live page actually IS, observed in the DOM by
    the generator's probe (`bin/generate_answers.py`, `probe_date_controls`).

    The engine's captured `Field` cannot carry this: the vendor form schema
    declares `type: date` and NOTHING about the ORDER the box wants
    (`engine/kernel/contracts.py` has no placeholder/pattern attribute, and the
    kernel is frozen). Workable's live box is an `input[type=text]` with
    `placeholder="DD/MM/YYYY"`, and its apply page is a client-rendered SPA whose
    raw HTML carries no control at all, so the order exists ONLY in the hydrated
    DOM. It is therefore DERIVED there, once, and carried here.

    `mechanism` is what the control IS (see `_MECHANISM_*`), `date_format` the
    strftime string DERIVED from it (None when nothing derivable was found), and
    `evidence` the raw attribute the derivation read, kept so a human can audit
    the claim rather than trust it.

    A wrong ORDER is the correctness trap of this whole resolver: 05/08 and 08/05
    are six months apart, typed with equal confidence. Nothing here is ever
    assumed, and an underivable control resolves to NO FILL (see
    `_start_date_verdict`), never to a guessed order.
    """
    key: str
    mechanism: str
    date_format: str | None = None
    evidence: str = ""


@dataclass
class GeneratedAnswers:
    vendor: str
    slug: str
    job_id: str
    posting_lang: str
    answers: list[GeneratedAnswer] = field(default_factory=list)
    tos_forbidden: list[TosForbidden] = field(default_factory=list)
    # -- W5.1d additive extension (owner rulings 12, 13, 14). Every field
    # defaults, and `schema_version` stays "1": a document written before this
    # wave still loads, and simply resolves no policy answer (fail closed).
    #
    # These three carry the POSTING CONTEXT the resolvers need and the engine
    # cannot otherwise reach. `w5_accept.py:71` is the ONLY caller of
    # `apply_content_overlay` and it is FROZEN: it passes exactly
    # (values, fieldmap, ssot, generated, posting_lang). A new keyword argument on
    # the overlay would therefore be a LIVE NO-OP -- nothing would ever pass it --
    # so the context rides on the ONE object the harness already loads from disk.
    posting_location: str = ""
    discovery_source: str = ""
    date_controls: list[DateControl] = field(default_factory=list)


def load_generated_answers(path: str | Path) -> GeneratedAnswers:
    """Parse a generated-answers YAML file into `GeneratedAnswers`.

    Strict by design: an unreadable file, a non-mapping document, an unknown
    `schema_version`, a missing mandatory key, an answer with neither key nor
    label, or an answer with an empty value all raise `ContentSchemaError`.
    Tolerating an ABSENT file is NOT this function's job: a caller with no
    generation for a posting passes `generated=None` to the overlay instead of
    calling this at all.
    """
    raw = _read_document(path)
    version = str(raw.get("schema_version", ""))
    if version != CONTENT_VERSION:
        raise ContentSchemaError(
            f"generated answers: schema_version must be {CONTENT_VERSION!r}, "
            f"got {version!r}")
    for key in ("vendor", "slug", "job_id"):
        if not str(raw.get(key) or "").strip():
            raise ContentSchemaError(f"generated answers: missing {key!r}")

    return GeneratedAnswers(
        vendor=str(raw["vendor"]),
        slug=str(raw["slug"]),
        job_id=str(raw["job_id"]),
        posting_lang=str(raw.get("posting_lang") or "en"),
        answers=_parse_answers(raw.get("answers") or []),
        tos_forbidden=_parse_forbidden(raw.get("tos_forbidden") or []),
        posting_location=str(raw.get("posting_location") or "").strip(),
        discovery_source=str(raw.get("discovery_source") or "").strip(),
        date_controls=_parse_date_controls(raw.get("date_controls") or []))


def _read_document(path: str | Path) -> dict:
    """The YAML document as a mapping, or `ContentSchemaError`."""
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except OSError as exc:
        raise ContentSchemaError(f"generated answers unreadable: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContentSchemaError(f"generated answers not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContentSchemaError("generated answers: top level must be a mapping")
    return raw


def _parse_answers(raw_answers) -> list[GeneratedAnswer]:
    """The `answers` block: every entry needs a key or a label, and a non-empty
    value (an empty one is a lost question, not an answer)."""
    if not isinstance(raw_answers, list):
        raise ContentSchemaError("generated answers: 'answers' must be a list")
    answers: list[GeneratedAnswer] = []
    for entry in raw_answers:
        if not isinstance(entry, dict):
            raise ContentSchemaError("generated answers: each answer must be a mapping")
        key = entry.get("key")
        label = entry.get("label")
        if not (str(key or "").strip() or str(label or "").strip()):
            raise ContentSchemaError(
                "generated answers: each answer needs a key or a label")
        value = entry.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ContentSchemaError(
                f"generated answers: answer {key or label!r} has an empty value")
        answers.append(GeneratedAnswer(
            key=str(key) if key is not None else None,
            label=str(label) if label is not None else None,
            value=value))
    return answers


def _parse_forbidden(raw_forbidden) -> list[TosForbidden]:
    """The `tos_forbidden` block: every entry needs the label the overlay joins
    on, so a forbidden question can be reported (never hidden) by name."""
    if not isinstance(raw_forbidden, list):
        raise ContentSchemaError("generated answers: 'tos_forbidden' must be a list")
    forbidden: list[TosForbidden] = []
    for entry in raw_forbidden:
        if not isinstance(entry, dict) or not str(entry.get("label") or "").strip():
            raise ContentSchemaError(
                "generated answers: each tos_forbidden entry needs a label")
        forbidden.append(TosForbidden(
            label=str(entry["label"]),
            reason=str(entry.get("reason") or "").strip() or "tos forbids"))
    return forbidden


def _parse_date_controls(raw_controls) -> list[DateControl]:
    """The `date_controls` block: the generator's DOM observation of each date
    control, one entry per field key.

    A `date_format` the loader does not RECOGNIZE (`is_supported_date_format`) is
    refused outright rather than carried: the format is the one value in this
    document that decides WHICH DAY gets typed into a real application, and a
    format nothing validated is exactly how "%m/%d/%Y" reaches a box that wanted
    "%d/%m/%Y". A control with no derivable format is legitimate and carries
    `date_format: null`; it simply never fills (`_start_date_verdict`).
    """
    if not isinstance(raw_controls, list):
        raise ContentSchemaError("generated answers: 'date_controls' must be a list")
    controls: list[DateControl] = []
    for entry in raw_controls:
        if not isinstance(entry, dict) or not str(entry.get("key") or "").strip():
            raise ContentSchemaError(
                "generated answers: each date_controls entry needs a key")
        fmt = entry.get("date_format")
        fmt = str(fmt).strip() if fmt is not None and str(fmt).strip() else None
        if fmt is not None and not is_supported_date_format(fmt):
            raise ContentSchemaError(
                f"generated answers: date_controls entry {entry['key']!r} carries "
                f"an unsupported date_format {fmt!r}")
        controls.append(DateControl(
            key=str(entry["key"]),
            mechanism=str(entry.get("mechanism") or "").strip() or MECHANISM_UNPROBED,
            date_format=fmt,
            evidence=str(entry.get("evidence") or "").strip()))
    return controls


# -- canned routing table -----------------------------------------------------

# Substring keywords matched against the NORMALIZED field label, kernel-matcher
# style: the first row with any substring hit wins, and the row applies only when
# one of its candidate SSOT paths resolves (the kernel MISSING sentinel is
# respected). Order is load-bearing. Each row carries a TUPLE of candidate paths
# so the verbose relocation dropdown can fall back to the plain relocation string
# when the dedicated dropdown answer was never seeded.
_CONTENT_MATCHERS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    # ("how did you hear ...") is DELIBERATELY not here: it is a per-application
    # POLICY (`_how_heard_verdict`), which maps the canned default onto the
    # vendor's OWN option list (exact -> unambiguous containment -> Other) rather
    # than leaning on the generic option matcher, so the same routing serves all
    # four vendors (RS-c).
    # Dropdown-first, like relocation below: the ATTENDANCE POLICY string is a
    # location-conditional paragraph (correct for a free-text box, unmappable onto
    # a Yes/No control), so an exact-option scalar leads and the paragraph is the
    # free-text fallback. Live evidence (anthropic 5164820008, 2026-07-13): the
    # paragraph alone produced NO_OPTION_MATCH on a ['Yes','No'] select and the
    # required field stayed unfilled. Owner ruling 2026-07-13 seeded the scalar.
    (("in-person", "in person", "in-office", "in office", "in the office",
      "on-site", "onsite"),
     ("canned_answers.in_office_dropdown",
      "canned_answers.in_office_attendance")),
    # Deliberately NOT keyed on the bare "applied to": that substring also carries
    # "Which team have you applied to?", a SHORT-TEXT question whose answer is a
    # team name, and the canned route would fill it with the previously-applied
    # "No". The keywords below all name the have-you-applied-BEFORE question.
    # Dropdown-first for the same reason as above: previously_applied_default is a
    # PROCESS NOTE ("answered from the application ledger"), not an answer, so it
    # can never satisfy a Yes/No control (live: NO_OPTION_MATCH, field unfilled).
    (("interviewed", "previously applied", "applied before"),
     ("canned_answers.previously_interviewed_dropdown",
      "canned_answers.previously_applied_default")),
    (("relocat",),
     ("canned_answers.relocation_dropdown",
      "canned_answers.willing_to_relocate")),
]


# == SSOT POLICY RESOLVERS (W5.1d; owner rulings 12, 13, 14) ==================
#
# Three POLICIES that no seeded string can express, because each depends on
# something the SSOT cannot know at seeding time: WHEN the script fires, WHERE
# the posting is, and WHETHER the form demands the answer.
#
# They live here, in the content channel, for one structural reason: this is the
# only surface that runs PER APPLICATION and knows both the CONTROL (from the
# fieldmap) and the POSTING (from the generated document). The kernel knows the
# form but not the posting, and it is frozen.
#
# All three FAIL CLOSED. A resolver that cannot answer TRUTHFULLY leaves the
# field unfilled and reports a gap (owner ruling 10: gaps are surfaced, never
# invented). An empty box is a question; a confidently wrong answer is a lie the
# owner sends to an employer under their own name.

# What a date control IS, as the generator's DOM probe found it.
MECHANISM_NATIVE_DATE = "native_date"    # input[type=date]: value is ISO, always
MECHANISM_TEXT_ENTRY = "text_entry"      # typed text, order taken from the control
MECHANISM_PICKER_ONLY = "picker_only"    # readonly box: only a click can set it
MECHANISM_PLAIN_TEXT = "plain_text"      # no date affordance at all: wants prose
MECHANISM_UNPROBED = "unprobed"          # the probe never saw this control

# The skip reasons the policy resolvers record. Single constants, like
# NO_OPTION_MATCH: the acceptance gate and the vendor loops key on the strings.
NO_DATE_FORMAT = "date format not derivable from the control"
PICKER_NOT_DRIVEN = "date picker is click-only (W5.1c click-policy wave)"
NO_POSTING_LOCATION = "posting location absent"
LOCATION_NOT_PLACEABLE = "posting location not placeable"
CITY_CHOICE_NOT_DECISIVE = "posting location too coarse to choose a city"
NO_CANDIDATE_CITIES = "no candidate cities in the SSOT"
NO_DISCOVERY_SOURCE = "discovery source absent"
# RS-c: the how-did-you-hear mapper's skip reasons. Single constants like the
# rest: the acceptance gate and the vendor loops key on the strings.
HOW_HEARD_NO_DEFAULT = "how-did-you-hear: no canned default seeded"
HOW_HEARD_NO_OPTION = ("how-did-you-hear: canned value matches no option and no "
                       "Other fallback is available")
# H.2 (owner ruling 2026-07-20): the paired "if other, please specify" free-text
# box is only a truthful fill WHEN the how-heard primary actually resolved to its
# Other option. When the primary landed on a real option ("Job board") or no
# primary is on the form, a seeded specify value is CLEARED with this reason, not
# left to orphan an "Other" note beside a non-Other answer.
SPECIFY_CLEARED_NOT_OTHER = ("specify text cleared: how-heard primary did not "
                             "resolve to Other (owner ruling H.2 2026-07-20)")
# The referral box the form did not ask for. OWNED and left EMPTY: owning it is
# what stops the generic ladder behind the policy from volunteering an LLM-written
# "a member of your team referred me" into a box nobody asked (see
# `_referral_verdict`). Disowning it would hand it straight to that ladder.
REFERRAL_NOT_VOLUNTEERED = "optional referral question: nothing to volunteer"
# The referral box that demands a PERSON. A genuine content gap for the owner to
# answer, never a blank for the engine to fill: see `_referral_verdict`.
REFERRAL_WANTS_A_PERSON = ("referral question demands a person, not a source "
                           "class: content gap for the owner")

# The SSOT routes. The FACTS stay in the SSOT and are read from it; this module
# holds the POLICY, never a duplicate of the owner's data.
#
# `canned_answers.earliest_start_date` is deliberately NOT here. Owner ruling 12
# SUPERSEDES it for the purpose of answering a start-date CONTROL (the answer is
# TODAY, computed at fire time). The owner's datum is not deleted, it is simply
# not the route for this control.
NOTICE_PATH = "canned_answers.notice_period"
CANDIDATE_CITIES_PATH = "preferences.location_policy.allowed_cities"
# Ruling 17a: the owner's OWN seeded answer for a required, option-less referral
# box that demands a PERSON. Read literally, never composed; see
# `_referral_verdict`.
REFERRAL_PERSON_PATH = "canned_answers.referral_person_if_required"
# RS-c: the owner's canonical "how did you hear about us" answer, and the free-
# text value a form's "Other, please specify" box takes when the mapper falls to
# Other. Both are read LITERALLY: the default is mapped onto the vendor's own
# option list, never composed, and the specify value is the sibling field's own
# seeded answer (resolved by the kernel's exact-slug route), recorded here only
# so the Other choice is auditable.
HOW_HEARD_DEFAULT_PATH = "canned_answers.how_did_you_hear_default"
OTHER_SPECIFY_PATH = "canned_answers.if_other_please_specify_below"

# The tokens a date placeholder may be built from, longest first so "yyyy" is
# read before "yy" and "dd" before "d" would ever be considered. Deliberately a
# CLOSED set: an order is only ever DERIVED from one of these exact tokens, and
# anything else (a month NAME, a single "d", a locale word) is not derivable and
# fails closed. A format is not a thing to be clever about.
_DATE_TOKENS = (("yyyy", "%Y"), ("yy", "%y"), ("mm", "%m"), ("dd", "%d"))
_DATE_SEPARATORS = "/-. "
_DATE_FORMAT_RE = re.compile(r"^(?:%[dmyY]|[/\-. ])+$")

# The three-value referral vocabulary (owner ruling 14, verbatim: "we state
# Vendor | Linkedin | Other"). It names the CLASS OF SOURCE that surfaced the
# posting. It NEVER names a person: the engine has no referrer and inventing one
# would be a fabrication sent to an employer.
REFERRAL_VENDOR = "Vendor"
REFERRAL_LINKEDIN = "LinkedIn"
REFERRAL_OTHER = "Other"

# The vendor boards the engine discovers through. `engine/fetch.py` REFUSES any
# other source vendor ("LinkedIn/Indeed/Glassdoor are read-avoid"), so today every
# posting the engine surfaces is class Vendor by construction. LinkedIn and Other
# remain in the vocabulary because the SOURCE is data, not a constant: a posting
# reached by another route must still be able to say so truthfully.
_VENDOR_SOURCES = ("greenhouse", "lever", "ashby", "workable")

# Label families. These select WHICH POLICY a control belongs to. They NEVER
# decide the ANSWER: owner ruling 12 is explicit that "notice period" and "start
# date" INTERCHANGE vendor by vendor, so a control LABELLED "notice" may want a
# DATE and one labelled "when can you start" may want a DURATION. What the field
# receives is decided by the CONTROL (`_expects_a_date`), never by these words.
_START_DATE_KEYWORDS = ("when can you start", "when could you start",
                        "when would you be able to start", "start date",
                        "earliest start", "available to start", "availability date",
                        "commencement date", "when are you available to start")
_NOTICE_KEYWORDS = ("notice period", "notice")
_CITY_KEYWORDS = ("which city", "what city", "your city", "specify your city",
                  "city of residence", "current city", "nearest city",
                  "closest city", "city are you based", "city you are based")
_REFERRAL_KEYWORDS = ("referred by", "were you referred", "who referred you",
                      "referral source", "referred to apply", "name of referrer",
                      "referrer")
# RS-c: the how-did-you-hear family. Same substrings the canned table once carried
# for it, now owned by `_how_heard_verdict` (option mapping + Other fallback), so
# the routing lives in one place.
_HOW_HEARD_KEYWORDS = ("how did you hear", "how you heard", "where did you hear",
                       "how did you find")

# The date family, as ONE list. PUBLIC because `bin/generate_answers.py` probes
# exactly these controls on the live page, and the probe list must be a SUPERSET of
# the list the policy CLAIMS: a label the policy routes to `_start_date_verdict`
# but the probe never looked at arrives with `control=None`, the classification
# falls back to the vendor's SCHEMA (the very witness this wave exists to
# distrust), and a DD/MM/YYYY box declared `text` gets prose typed into it. The two
# lists were duplicated across the module boundary and had already drifted (bare
# "notice" was claimed here and never probed there), so there is now exactly one
# (rules/15 DRY; rules/20 single source of truth across tool boundaries), and
# `test_the_date_probe_list_is_a_superset_of_the_policy_list` pins it.
POLICY_DATE_KEYWORDS = _START_DATE_KEYWORDS + _NOTICE_KEYWORDS

# Places, and how far a posting sitting on one of them could really be from it.
# PUBLIC geography (WGS84, 2dp), not owner data: the owner's OWN cities are read
# from the SSOT at fill time (`CANDIDATE_CITIES_PATH`) and only their coordinates
# are looked up here.
#
# `radius_km` is the entry's POSITIONAL UNCERTAINTY: a city is where it says it is
# (25 km), a country is anywhere inside itself. It is what makes the choice
# HONEST: a two-way choice is only made when it survives that uncertainty
# (`_nearest_city`), so "Italy" -- where the true answer genuinely depends on
# which Italian city -- resolves to a GAP rather than a coin flip dressed as a
# computation.
#
# SEEDED, not exhaustive. A place that is not here is NOT guessed: it is reported
# as a gap and the table grows from the report (owner ruling 10). Left-to-right
# matching means the CITY in "Atlanta, Georgia, United States" wins over the
# country, which is why no name that collides with a US state (Georgia) is
# carried here.
_CITY_RADIUS_KM = 25.0

_GAZETTEER: dict[str, tuple[float, float, float]] = {
    # Italy
    "milan": (45.46, 9.19, _CITY_RADIUS_KM),
    "milano": (45.46, 9.19, _CITY_RADIUS_KM),
    "bologna": (44.49, 11.34, _CITY_RADIUS_KM),
    "rome": (41.90, 12.50, _CITY_RADIUS_KM),
    "roma": (41.90, 12.50, _CITY_RADIUS_KM),
    "turin": (45.07, 7.69, _CITY_RADIUS_KM),
    "torino": (45.07, 7.69, _CITY_RADIUS_KM),
    "florence": (43.77, 11.26, _CITY_RADIUS_KM),
    "firenze": (43.77, 11.26, _CITY_RADIUS_KM),
    "naples": (40.85, 14.27, _CITY_RADIUS_KM),
    "napoli": (40.85, 14.27, _CITY_RADIUS_KM),
    "venice": (45.44, 12.32, _CITY_RADIUS_KM),
    "venezia": (45.44, 12.32, _CITY_RADIUS_KM),
    "genoa": (44.41, 8.93, _CITY_RADIUS_KM),
    "genova": (44.41, 8.93, _CITY_RADIUS_KM),
    "padua": (45.41, 11.88, _CITY_RADIUS_KM),
    "verona": (45.44, 10.99, _CITY_RADIUS_KM),
    "modena": (44.65, 10.93, _CITY_RADIUS_KM),
    "brescia": (45.54, 10.22, _CITY_RADIUS_KM),
    "bergamo": (45.70, 9.67, _CITY_RADIUS_KM),
    "trieste": (45.65, 13.78, _CITY_RADIUS_KM),
    "pisa": (43.72, 10.40, _CITY_RADIUS_KM),
    "bari": (41.12, 16.87, _CITY_RADIUS_KM),
    "catania": (37.50, 15.09, _CITY_RADIUS_KM),
    "palermo": (38.12, 13.36, _CITY_RADIUS_KM),
    # Europe
    "london": (51.51, -0.13, _CITY_RADIUS_KM),
    "cambridge": (52.20, 0.12, _CITY_RADIUS_KM),
    "manchester": (53.48, -2.24, _CITY_RADIUS_KM),
    "edinburgh": (55.95, -3.19, _CITY_RADIUS_KM),
    "paris": (48.86, 2.35, _CITY_RADIUS_KM),
    "lyon": (45.76, 4.84, _CITY_RADIUS_KM),
    "berlin": (52.52, 13.40, _CITY_RADIUS_KM),
    "munich": (48.14, 11.58, _CITY_RADIUS_KM),
    "hamburg": (53.55, 9.99, _CITY_RADIUS_KM),
    "frankfurt": (50.11, 8.68, _CITY_RADIUS_KM),
    "amsterdam": (52.37, 4.90, _CITY_RADIUS_KM),
    "brussels": (50.85, 4.35, _CITY_RADIUS_KM),
    "zurich": (47.38, 8.54, _CITY_RADIUS_KM),
    "geneva": (46.20, 6.14, _CITY_RADIUS_KM),
    "basel": (47.56, 7.59, _CITY_RADIUS_KM),
    "lausanne": (46.52, 6.63, _CITY_RADIUS_KM),
    "zug": (47.17, 8.52, _CITY_RADIUS_KM),
    "vienna": (48.21, 16.37, _CITY_RADIUS_KM),
    "prague": (50.08, 14.44, _CITY_RADIUS_KM),
    "warsaw": (52.23, 21.01, _CITY_RADIUS_KM),
    "madrid": (40.42, -3.70, _CITY_RADIUS_KM),
    "barcelona": (41.39, 2.17, _CITY_RADIUS_KM),
    "lisbon": (38.72, -9.14, _CITY_RADIUS_KM),
    "dublin": (53.35, -6.26, _CITY_RADIUS_KM),
    "stockholm": (59.33, 18.07, _CITY_RADIUS_KM),
    "copenhagen": (55.68, 12.57, _CITY_RADIUS_KM),
    "oslo": (59.91, 10.75, _CITY_RADIUS_KM),
    "helsinki": (60.17, 24.94, _CITY_RADIUS_KM),
    "tallinn": (59.44, 24.75, _CITY_RADIUS_KM),
    "athens": (37.98, 23.73, _CITY_RADIUS_KM),
    "bucharest": (44.43, 26.10, _CITY_RADIUS_KM),
    "budapest": (47.50, 19.04, _CITY_RADIUS_KM),
    # Rest of world
    "new york": (40.71, -74.01, _CITY_RADIUS_KM),
    "san francisco": (37.77, -122.42, _CITY_RADIUS_KM),
    "seattle": (47.61, -122.33, _CITY_RADIUS_KM),
    "boston": (42.36, -71.06, _CITY_RADIUS_KM),
    "chicago": (41.88, -87.63, _CITY_RADIUS_KM),
    "austin": (30.27, -97.74, _CITY_RADIUS_KM),
    "los angeles": (34.05, -118.24, _CITY_RADIUS_KM),
    "atlanta": (33.75, -84.39, _CITY_RADIUS_KM),
    "denver": (39.74, -104.98, _CITY_RADIUS_KM),
    "miami": (25.76, -80.19, _CITY_RADIUS_KM),
    "toronto": (43.65, -79.38, _CITY_RADIUS_KM),
    "vancouver": (49.28, -123.12, _CITY_RADIUS_KM),
    "montreal": (45.50, -73.57, _CITY_RADIUS_KM),
    "tel aviv": (32.09, 34.78, _CITY_RADIUS_KM),
    "dubai": (25.20, 55.27, _CITY_RADIUS_KM),
    "singapore": (1.35, 103.82, _CITY_RADIUS_KM),
    "tokyo": (35.68, 139.65, _CITY_RADIUS_KM),
    "bangalore": (12.97, 77.59, _CITY_RADIUS_KM),
    "sydney": (-33.87, 151.21, _CITY_RADIUS_KM),
    # Countries: a centroid and the country's own radius. Most will NOT be
    # decisive, and that is the point: a country-only posting whose answer
    # depends on which city is a GAP, not a guess.
    "italy": (42.80, 12.50, 400.0),
    "united kingdom": (54.00, -2.50, 400.0),
    "united states": (39.83, -98.58, 1500.0),
    "germany": (51.00, 10.00, 350.0),
    "france": (46.60, 2.50, 400.0),
    "spain": (40.20, -3.60, 400.0),
    "netherlands": (52.20, 5.30, 120.0),
    "switzerland": (46.80, 8.20, 120.0),
    "ireland": (53.20, -8.00, 180.0),
    "portugal": (39.50, -8.00, 200.0),
    "belgium": (50.60, 4.60, 100.0),
    "austria": (47.60, 14.10, 200.0),
    "canada": (56.00, -96.00, 2000.0),
    "india": (22.00, 79.00, 1300.0),
    "israel": (31.50, 34.90, 110.0),
    "japan": (36.50, 138.00, 800.0),
    "australia": (-25.00, 134.00, 1800.0),
}

# Words that describe a WORK ARRANGEMENT, not a place. Dropped before matching so
# "Milan, Italy (Remote)" still resolves to Milan; a location made of NOTHING but
# these ("Remote") places nowhere and is reported as a gap, which is the truth.
_NON_PLACE_TOKENS = frozenset((
    "remote", "hybrid", "onsite", "on-site", "on site", "office", "anywhere",
    "flexible", "work from home", "wfh", "distributed", "global", "worldwide",
    "emea", "apac", "amer", "europe", "eu", "worldwide remote",
))

_PAREN_RE = re.compile(r"\([^)]*\)")
_LOCATION_SPLIT_RE = re.compile(r"[,;/|]| - ")

EARTH_RADIUS_KM = 6371.0


def is_supported_date_format(fmt: str) -> bool:
    """True iff `fmt` is a date format this module is willing to TYPE INTO A REAL
    APPLICATION: built only from the closed token set (`_DATE_TOKENS`) and the
    separators, carrying exactly one day, one month and one year token, and
    round-tripping through `strptime`.

    The one validation that matters in this whole wave. A format is what turns
    "the 8th of May" into "05/08" or "08/05", and the two are six months apart.
    Anything not recognizably built from dd/mm/yyyy is refused, and the field
    stays empty. Deliberately NOT a strftime passthrough: `%c`, `%x` and a bare
    `%Y` are all "valid" strftime and all wrong here.
    """
    fmt = str(fmt or "")
    if not fmt or not _DATE_FORMAT_RE.match(fmt):
        return False
    if fmt.count("%d") != 1 or fmt.count("%m") != 1:
        return False
    if fmt.count("%Y") + fmt.count("%y") != 1:
        return False
    probe = date(2026, 1, 2)
    try:
        return datetime.strptime(probe.strftime(fmt), fmt).date() == probe
    except ValueError:
        return False


def date_format_from_placeholder(placeholder: str) -> str | None:
    """The strftime format a control's own placeholder DECLARES, or None.

    "DD/MM/YYYY" -> "%d/%m/%Y". "MM/DD/YYYY" -> "%m/%d/%Y". "YYYY-MM-DD" ->
    "%Y-%m-%d". Anything that is not built from the closed token set -- a month
    name, a locale word, a bare "Date" -- yields None, and None means the field is
    NOT FILLED.

    The ONE definition both ends of the date channel key on: the generator's probe
    derives with it (`bin/generate_answers.py`) and the loader validates with it
    (`_parse_date_controls`), so the format the probe writes is by construction a
    format the overlay will accept. Mirroring it would let the two drift, and a
    drift here is a wrong DAY typed into a real application.
    """
    text = _WS_RE.sub(" ", str(placeholder or "").casefold()).strip()
    if not text:
        return None
    out: list[str] = []
    index = 0
    while index < len(text):
        for token, code in _DATE_TOKENS:
            if text.startswith(token, index):
                out.append(code)
                index += len(token)
                break
        else:
            if text[index] not in _DATE_SEPARATORS:
                return None
            out.append(text[index])
            index += 1
    fmt = "".join(out)
    return fmt if is_supported_date_format(fmt) else None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points.

    Deliberately the whole of the geography: no geocoding service, no network
    call, no projection library. A two-way choice between two cities 200 km apart
    does not need more, and a dependency that reaches the network at fill time
    would be a new failure mode on the page.
    """
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def place_location(location: str) -> tuple[float, float, float] | None:
    """The posting's location as (lat, lon, radius_km), or None when it names no
    place this module can place.

    Read LEFT TO RIGHT, most specific first: "Atlanta, Georgia, United States"
    resolves on ATLANTA (25 km), not on the country (1500 km), so the choice is
    made from the finest place the posting actually named. A work-arrangement word
    is not a place (`_NON_PLACE_TOKENS`), so "Remote" places nowhere -- which is
    the honest answer, not a reason to guess a city.
    """
    text = _PAREN_RE.sub(" ", str(location or ""))
    for part in _LOCATION_SPLIT_RE.split(text):
        name = _normalize(part).replace(".", "").strip()
        if not name or name in _NON_PLACE_TOKENS:
            continue
        found = _GAZETTEER.get(name)
        if found is not None:
            return found
    return None


def nearest_city(location: str, cities: list[str]) -> tuple[str | None, str]:
    """Whichever of `cities` is NEAREST the posting's location, as (city, reason).

    The computation owner ruling 13 asks for, and its own honesty check. The
    choice is made ONLY when it survives the location's positional uncertainty:
    half the difference of the two distances is a lower bound on how far the
    posting is from the two cities' bisector, so when that half-difference exceeds
    the location's radius, EVERY point the location could really be still picks the
    same city. When it does not, the location is too coarse to decide ("Italy":
    the true answer depends on which Italian city) and the field is reported as a
    gap rather than answered with a coin flip.

    Fails closed on every other unknown too: no location, an unplaceable one, no
    candidate cities, or a candidate city whose coordinates are not seeded.
    """
    if not str(location or "").strip():
        return None, NO_POSTING_LOCATION
    if not cities:
        return None, NO_CANDIDATE_CITIES
    here = place_location(location)
    if here is None:
        return None, LOCATION_NOT_PLACEABLE

    distances: list[tuple[float, str]] = []
    for city in cities:
        seat = _GAZETTEER.get(_normalize(city))
        if seat is None:
            return None, f"{LOCATION_NOT_PLACEABLE}: no coordinates for {city!r}"
        distances.append((haversine_km((here[0], here[1]), (seat[0], seat[1])), city))
    distances.sort()
    if len(distances) == 1:
        return distances[0][1], ""
    margin = (distances[1][0] - distances[0][0]) / 2
    if margin <= here[2]:
        return None, CITY_CHOICE_NOT_DECISIVE
    return distances[0][1], ""


def referral_class(discovery_source: str) -> str | None:
    """The posting's discovery source as one of {Vendor, LinkedIn, Other}, or None
    when the source is unknown.

    Owner ruling 14's vocabulary. It states HOW THE POSTING WAS FOUND, which is a
    fact the engine holds. It is NEVER a person's name: the owner was not referred,
    and an engine that invents a referrer is fabricating a relationship to an
    employer. An UNKNOWN source yields None, and the field stays empty.
    """
    source = _normalize(discovery_source)
    if not source:
        return None
    if source in _VENDOR_SOURCES or source.endswith(" board"):
        return REFERRAL_VENDOR
    if "linkedin" in source:
        return REFERRAL_LINKEDIN
    return REFERRAL_OTHER


def is_referral_question(label: str) -> bool:
    """True iff this label asks HOW/BY WHOM the applicant was referred.

    PUBLIC, and the single definition BOTH ends of the content channel key on: the
    overlay uses it to pick the referral POLICY (`_policy_verdict`), and the
    offline generator uses it to keep the question away from a model altogether
    (`bin/generate_answers.py`, `questions_from_fieldmap`).

    The generator side is not belt-and-braces, it is the root fix for a live
    fabrication: an essay-shaped referral box was sent to a model, which wrote "a
    member of your engineering team referred me" -- a relationship that does not
    exist -- and the overlay then typed it into an application. A referral is a FACT
    about the owner, never a thing to compose. No model is asked about one, and the
    only referral answer the engine will ever type is the CLASS of source that
    surfaced the posting, into a control that offers that class as an option.
    """
    return any(word in _normalize(label) for word in _REFERRAL_KEYWORDS)


@dataclass
class PolicyVerdict:
    """What a policy resolver says about ONE field.

    `owned` is the load-bearing bit, and OWNING A FIELD IS TERMINAL: the field is
    offered the policy's OWN value and NOTHING ELSE (`_owned_candidate`), so a
    policy that claimed a field and then declined to answer it (`value is None`),
    or whose answer its own control rejects (no option match, over max length),
    leaves the field EMPTY and REPORTED. The canned table and the generated answer
    are never consulted for it.

    Terminal because the alternative fabricates. A policy declines precisely where
    the truthful answer is unavailable -- a referral box that wants a PERSON when
    nobody referred the owner -- and the generic ladder behind it does not know
    that: it matches loose label substrings and would fill the very field the
    policy just refused, typing a canned scalar into a date box or a generated
    person's name under "Who referred you?". An unfilled field is a gap a human
    closes; a fabricated referrer is a lie already sent to an employer.
    """
    owned: bool = False
    value: str | None = None
    source: str = ""
    reason: str = ""


def _today() -> date:
    """The current date, at the moment the fill runs. The seam the tests replace:
    owner ruling 12 says the answer is TODAY, so there is no literal to assert
    against and nothing to freeze but the clock itself."""
    return date.today()


def _expects_a_date(fld: Field, control: DateControl | None) -> bool:
    """True iff this CONTROL wants a DATE (rather than a duration or free prose).

    Decided by the CONTROL, never by the LABEL. Owner ruling 12 is explicit that
    "notice period" and "start date" interchange vendor by vendor, so a box
    labelled "notice" may want a date and one labelled "when can you start" may
    want "immediately". The label picks the POLICY; the control picks the TYPE.

    The live DOM outranks the schema when the probe saw the control: workable
    declares `type: date` in its form API and renders a typed text box, and a
    vendor that declares a date and renders a PLAIN text box wants prose. With no
    probe, the vendor's own schema (`norm_type`/`type`) is the only witness left.
    """
    if control is not None and control.mechanism != MECHANISM_UNPROBED:
        return control.mechanism in (MECHANISM_NATIVE_DATE, MECHANISM_TEXT_ENTRY,
                                     MECHANISM_PICKER_ONLY)
    return (str(fld.norm_type or "").upper() == "DATE"
            or str(fld.type or "").strip().lower() == "date")


def _start_date_verdict(fld: Field, ssot: SSOT,
                        control: DateControl | None) -> PolicyVerdict:
    """Owner ruling 12: a control asking WHEN the owner can start.

    A control that wants a DATE gets TODAY, computed at fire time and rendered in
    the order the control ITSELF declared. Never a literal: a seeded date is a
    false fact about the owner's availability on every day after the day it was
    seeded.

    A control that wants a DURATION gets the owner's notice period, read from the
    SSOT (`NOTICE_PATH`), which already says there is none. The fact stays in the
    SSOT; only the routing is here.

    Fails closed, twice over, because a WRONG DATE IS WORSE THAN AN EMPTY BOX:
    no derivable order means no fill (`NO_DATE_FORMAT`), and a picker that can only
    be CLICKED means no fill either (`PICKER_NOT_DRIVEN`) -- the content channel
    supplies values and never drives the page, so that control belongs to the
    W5.1c click-policy wave, and it is reported by name rather than typed into
    blind.
    """
    if _expects_a_date(fld, control):
        if control is not None and control.mechanism == MECHANISM_PICKER_ONLY:
            return PolicyVerdict(owned=True, reason=PICKER_NOT_DRIVEN)
        fmt = control.date_format if control is not None else None
        if not fmt or not is_supported_date_format(fmt):
            return PolicyVerdict(owned=True, reason=NO_DATE_FORMAT)
        return PolicyVerdict(owned=True, value=_today().strftime(fmt),
                             source=f"policy:start_date:{fmt}")
    value = _scalar_text(ssot.get(NOTICE_PATH))
    if value is None:
        return PolicyVerdict(owned=True,
                             reason=f"no literal SSOT value at {NOTICE_PATH}")
    return PolicyVerdict(owned=True, value=value, source=f"canned:{NOTICE_PATH}")


def _city_verdict(ssot: SSOT, generated: GeneratedAnswers | None) -> PolicyVerdict:
    """Owner ruling 13: a control asking WHICH CITY.

    Answered with whichever of the owner's own cities (read from the SSOT, never
    duplicated here) is NEAREST the posting's location. A computation, not a
    default: the posting's location comes from the vendor's own API, and the
    choice is a great-circle distance.

    Fails closed when the posting's location is absent, unplaceable, or too coarse
    to decide. There is no default city, and inventing one would tell an employer
    the owner lives somewhere they may not.
    """
    location = generated.posting_location if generated is not None else ""
    raw = ssot.get(CANDIDATE_CITIES_PATH)
    cities = [str(c).strip() for c in raw if str(c).strip()] if isinstance(raw, list) else []
    city, reason = nearest_city(location, cities)
    if city is None:
        return PolicyVerdict(owned=True, reason=reason)
    return PolicyVerdict(owned=True, value=city,
                         source=f"policy:nearest_city:{CANDIDATE_CITIES_PATH}")


def _referral_verdict(fld: Field, ssot: SSOT,
                      generated: GeneratedAnswers | None) -> PolicyVerdict:
    """Owner ruling 14, verbatim: "IFF this is a needed answer, we state Vendor |
    Linkedin | Other else: do nothing."

    The ANSWER is decided by the CONTROL, never by the label. The label only says
    the question is about a referral; whether the box wants a SOURCE CLASS or a
    PERSON is a property of the control, and the evidence is its OPTION LIST:

    * NOT REQUIRED -> OWNED, and left EMPTY. Owned, not disowned: `owned=False`
      does not mean "do nothing", it means "no policy claims this field", and the
      field then falls through to the generic canned/generated ladder
      (`_candidates`), which is exactly how an LLM-written "a member of your
      engineering team referred me" got typed into an OPTIONAL essay-shaped
      referral box on a real posting. Owning it stops the ladder dead
      (`_owned_candidate`), so the box the form did not ask about stays empty and
      is reported. Nothing is volunteered.
    * REQUIRED, and the control OFFERS OPTIONS -> the class of source that surfaced
      the posting, which the option list is asking for (Vendor / LinkedIn / Other
      or the vendor's own wording). The value still has to map onto exactly one of
      the field's own options (`_fit_to_options`) or the field stays empty, and
      because owning is TERMINAL nothing behind the policy may fill it either: a
      control whose options are PEOPLE takes no `Vendor`, and a generated answer
      naming one of those people can never reach it.
    * REQUIRED, and the control has NO OPTIONS -> a free box under "who were you
      referred by?" wants a PERSON, which the engine cannot compose (ruling 14) but
      the OWNER can seed (ruling 17a): `REFERRAL_PERSON_PATH` in the SSOT, read
      literally and never composed. If the owner seeded a non-empty answer there,
      it is the value, verbatim. If the owner did NOT seed one, the field is a GAP:
      the owner was not referred by anyone, and typing the source class `Vendor`
      there would answer a who-question with a source token AND assert a referral
      that never happened; inventing a name would fabricate a relationship with an
      employer; typing "N/A" would be the engine deciding, on its own authority,
      what the owner says to an employer. So the field is left EMPTY and reported
      by name (`REFERRAL_WANTS_A_PERSON`), which is the honest output: a gap is a
      question the owner answers, a wrong value is a lie already sent.

    Required-ness is read from the vendor SCHEMA (`fld.required`), which is the
    only witness the fieldmap carries; the live DOM agreed with it on the target
    posting (`required=""`, `aria-required="true"`). The residual exposure is
    bounded on BOTH sides and cannot fabricate: a schema that under-declares
    required-ness leaves a required box EMPTY (a gap, the safe direction), and one
    that over-declares it can at worst state the TRUE source class into a control
    that offers that very class as an option. Neither invents a referrer.
    """
    if not fld.required:
        return PolicyVerdict(owned=True, reason=REFERRAL_NOT_VOLUNTEERED)
    if not fld.options:
        seeded = _scalar_text(ssot.get(REFERRAL_PERSON_PATH))
        if seeded is not None:
            return PolicyVerdict(owned=True, value=seeded,
                                 source=f"canned:{REFERRAL_PERSON_PATH}")
        return PolicyVerdict(owned=True, reason=REFERRAL_WANTS_A_PERSON)
    source = generated.discovery_source if generated is not None else ""
    answer = referral_class(source)
    if answer is None:
        return PolicyVerdict(owned=True, reason=NO_DISCOVERY_SOURCE)
    return PolicyVerdict(owned=True, value=answer,
                         source=f"policy:referral:{_normalize(source)}")


def _how_heard_verdict(fld: Field, ssot: SSOT) -> PolicyVerdict:
    """RS-c: a "how did you hear about us" control, mapped DETERMINISTICALLY from
    the owner's canned default onto the vendor's own option list.

    The default (`HOW_HEARD_DEFAULT_PATH`) resolves to EXACTLY ONE option:
    (1) a case-insensitive exact match; (2) an unambiguous normalized CONTAINMENT
    match (exactly one option contains the value or is contained by it); (3)
    failing both, the form's "Other" option WHEN one exists AND the owner seeded
    the free-text specify answer (`OTHER_SPECIFY_PATH`) that the sibling specify
    field then carries; (4) otherwise the field is left honestly unfilled and
    reported. Live defect (ashby elevenlabs, 2026-07): the canned value matched no
    ashby radio option and the required field stayed blank, while lever's own
    select carried the value directly.

    Shared across all four vendors because it lives in the content overlay, not a
    vendor plugin. An OPTION-LESS short-text box takes the canned value verbatim;
    an essay-shaped one is DISOWNED so the generated ladder answers it (a one-word
    canned scalar never lands in a write-your-answer box -- the module's standing
    invariant, keyed on the CONTROL via `is_free_text`)."""
    if is_free_text(fld.type, fld.norm_type, fld.max_length, fld.options):
        return PolicyVerdict(owned=False)
    default = _scalar_text(ssot.get(HOW_HEARD_DEFAULT_PATH))
    if default is None:
        return PolicyVerdict(owned=True, reason=HOW_HEARD_NO_DEFAULT)
    if not fld.options:
        return PolicyVerdict(owned=True, value=default,
                             source=f"canned:{HOW_HEARD_DEFAULT_PATH}")
    option = _map_onto_options(default, fld.options)
    if option is not None:
        return PolicyVerdict(owned=True, value=option,
                             source=f"canned:{HOW_HEARD_DEFAULT_PATH}")
    other = _other_option(fld.options)
    if other is not None and ssot.get(OTHER_SPECIFY_PATH) is not MISSING:
        return PolicyVerdict(owned=True, value=other,
                             source=f"canned:{HOW_HEARD_DEFAULT_PATH}:other")
    return PolicyVerdict(owned=True, reason=HOW_HEARD_NO_OPTION)


_OTHER_OPTION_RE = re.compile(r"other\b", re.I)


def _map_onto_options(value: str, options: list[str]) -> str | None:
    """Map `value` onto EXACTLY ONE option label, or None (RS-c steps 1-2).

    (1) a case-insensitive exact match; then (2) a normalized CONTAINMENT match,
    accepted ONLY when unambiguous -- exactly one option contains the value (or is
    contained by it). Zero or two-plus candidates at either step yields None
    (ambiguity is never guessed away), and the caller then tries the Other
    fallback or parks."""
    target = _normalize(value)
    if not target:
        return None
    exact = [opt for opt in options if _normalize(opt) == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    contained = [opt for opt in options
                 if (norm := _normalize(opt))
                 and (target in norm or norm in target)]
    return contained[0] if len(contained) == 1 else None


def _other_option(options: list[str]) -> str | None:
    """The single "Other" option in an option list, or None (no Other, or an
    ambiguous set with more than one). Matched on a leading "other" word so
    "Other", "Other (please specify)" and "Other:" all hit while "Another" and a
    label that merely contains "other" mid-word do not."""
    others = [opt for opt in options if _OTHER_OPTION_RE.match(_normalize(opt))]
    return others[0] if len(others) == 1 else None


def _policy_verdict(fld: Field, label: str, ssot: SSOT,
                    generated: GeneratedAnswers | None) -> PolicyVerdict:
    """The policy that owns this field, if any. First family wins.

    The label chooses the FAMILY (which question is being asked); each resolver
    then decides the ANSWER from the CONTROL and the POSTING. A field no family
    claims is returned unowned and takes the ordinary canned/generated ladder.
    """
    control = _date_control_for(fld, generated)
    if any(word in label for word in POLICY_DATE_KEYWORDS):
        return _start_date_verdict(fld, ssot, control)
    if any(word in label for word in _CITY_KEYWORDS):
        return _city_verdict(ssot, generated)
    if is_referral_question(label):
        return _referral_verdict(fld, ssot, generated)
    if any(word in label for word in _HOW_HEARD_KEYWORDS):
        return _how_heard_verdict(fld, ssot)
    return PolicyVerdict(owned=False)


def _date_control_for(fld: Field,
                      generated: GeneratedAnswers | None) -> DateControl | None:
    """The generator's DOM observation of THIS control, or None if it never saw it."""
    if generated is None:
        return None
    for control in generated.date_controls:
        if control.key == fld.key:
            return control
    return None


def _misrouted_kernel_dates(resolved: ResolvedValues, by_key: dict[str, Field],
                            generated: GeneratedAnswers | None
                            ) -> list[tuple[str, str]]:
    """Date controls the KERNEL already filled with something that is not a date.

    DETECTED, NEVER CORRECTED. The kernel's resolver matches "notice" on the LABEL
    alone (`engine/kernel/resolve.py:123`) and renders the SSOT's notice prose as
    free text (`resolve.py:672-675`), so a DATE-typed control labelled "Notice
    period" is filled with "None: available immediately." and lands in
    `resolved.fields` -- where this overlay, which is additive only, cannot legally
    touch it. That is a KERNEL defect (the kernel is frozen; W5.1d escalated it
    with this evidence), and the one thing worse than the defect would be hiding
    it. So it is reported by name and the value is left exactly as the kernel wrote
    it: no silent overwrite of a frozen surface's decision.
    """
    misrouted: list[tuple[str, str]] = []
    for value in resolved.fields:
        fld = by_key.get(value.key)
        if fld is None or value.asset is not None:
            continue
        control = _date_control_for(fld, generated)
        if not _expects_a_date(fld, control):
            continue
        formats = ([control.date_format] if control and control.date_format
                   else ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"])
        for fmt in formats:
            try:
                datetime.strptime(str(value.value), fmt)
                break
            except ValueError:
                continue
        else:
            misrouted.append((value.key,
                              "kernel filled a date control with a non-date value"))
    return misrouted


@dataclass
class OverlayReport:
    """What the overlay did. The evidence record the vendor loops and the
    acceptance gate will consume once the overlay is wired into them (see the
    module STATUS note: no production caller yet).

    `applied`: (field_key, source) with source "canned:<dotted path>",
    "generated:key", or "generated:label".
    `tos_forbidden`: field keys the ToS forbids us to answer (left unfilled ON
    PURPOSE, and reported by name so the gate can subtract them from the
    completeness denominator instead of the engine hiding them).
    `unresolved`: (field_key, reason) still skipped after the overlay ran.

    Policy-declined fields (`is_policy_declined`) and upload fields appear in
    NONE of the three lists: the overlay does not touch them, and each already
    has its own justified-skip route through the gate.

    `misrouted`: (field_key, reason) for a field the KERNEL filled with a value of
    the wrong TYPE for its control (today: a date box carrying prose, see
    `_misrouted_kernel_dates`). Reported, never corrected: the overlay is additive
    only and the kernel is frozen. It is a defect record for the owner, not a
    completeness claim. It is ALSO emitted to stderr as it is found
    (`_announce_misrouted`), because a record only a future caller might read is not
    a report: the overlay's one live caller (`w5_accept.py`, FROZEN) builds its
    result document from `applied` / `tos_forbidden` / `unresolved` and DROPS this
    list, so without the stderr line the detection would reach no human, no artefact
    and no gate -- detection without report, which is half of what §2 asked for. The
    line goes to STDERR, never stdout, so it cannot corrupt a caller that writes a
    JSON document there.
    """
    applied: list[tuple[str, str]] = field(default_factory=list)
    tos_forbidden: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    misrouted: list[tuple[str, str]] = field(default_factory=list)


def _how_heard_primary_is_other(fieldmap: FieldMap,
                                resolved: ResolvedValues) -> bool:
    """True iff a how-did-you-hear PRIMARY field resolved to its own "Other"
    option (owner ruling H.2, 2026-07-20).

    The `FieldValue` carries no policy source tag, so Other-ness is RE-DERIVED
    from the resolved VALUE: a how-heard field (label in `_HOW_HEARD_KEYWORDS`)
    picked Other exactly when its resolved value equals `_other_option` of its own
    option list. False when no how-heard primary is on the form, or none resolved
    to Other (conservative: with no Other context the paired specify box has
    nothing to specify)."""
    values = resolved.values
    for fld in fieldmap.fields:
        label = _normalize(fld.label)
        if not any(word in label for word in _HOW_HEARD_KEYWORDS):
            continue
        other = _other_option(fld.options)
        if other is not None and values.get(fld.key) == other:
            return True
    return False


def _clear_specify_when_primary_not_other(resolved: ResolvedValues,
                                          fieldmap: FieldMap) -> None:
    """H.2 cross-field post-pass: the "if other, please specify" box is a truthful
    fill ONLY when the how-heard primary resolved to Other. When it did not (or
    there is no primary), MOVE any seeded specify value from the filled fields to
    `skipped` with an honest reason, so a real answer like "Job board" never drags
    an orphan "Other" note onto the form. Mutates `resolved` in place.

    The specify field is identified the same way the kernel resolver FILLED it: its
    exact-slug guess is `OTHER_SPECIFY_PATH`. NOT-REQUIRED branch only (the specify
    box is optional); no conditional-display probing, no REQUIRED-case seeding."""
    if _how_heard_primary_is_other(fieldmap, resolved):
        return
    kept: list[FieldValue] = []
    for fv in resolved.fields:
        if _missing_path_guess(fv.label) == OTHER_SPECIFY_PATH:
            resolved.skipped.append((fv.key, SPECIFY_CLEARED_NOT_OTHER))
            continue
        kept.append(fv)
    resolved.fields[:] = kept


def apply_content_overlay(resolved: ResolvedValues, fieldmap: FieldMap, ssot: SSOT,
                          *, generated: GeneratedAnswers | None = None,
                          posting_lang: str = "en") -> OverlayReport:
    """Fill what the kernel resolver skipped, from canned SSOT routes and from
    pre-generated answers. MUTATES `resolved` IN PLACE and returns the report.

    Per skipped (key, reason), first hit wins:

    0. Unknown key, an upload field, or a POLICY-DECLINED field: untouched
       (uploads are the asset channel's business; a COMPLIANCE_EEOC /
       DEMOGRAPHIC / VOLUNTARY / `decline_allowed` field was skipped by the
       kernel ON POLICY and the overlay never overturns that -- see
       `is_policy_declined`).
    1. ToS-FORBIDDEN: the field label matches a `generated.tos_forbidden` entry.
       Recorded in the report; the value is NEVER filled and the field stays
       skipped.
    1b. POLICY (W5.1d, owner rulings 12/13/14): a start-date/notice control gets
       TODAY in the order the CONTROL itself declared, or the owner's (zero)
       notice period when the control wants a duration; a city control gets
       whichever of the owner's cities is nearest the POSTING; a REQUIRED referral
       control gets the class of source that surfaced the posting, and an OPTIONAL
       one is left alone. OWNING IS TERMINAL, which is how it fails closed: an
       owned field is offered the policy's OWN value and nothing else
       (`_owned_candidate`), so a policy that cannot answer TRUTHFULLY -- no value,
       or a value its own control rejects -- leaves the field EMPTY (reported in
       `unresolved`) and the routes below are never consulted for it. They would
       otherwise fill a date box with prose on a loose label match, or type a
       generated person's name into the referral box the resolver just refused.
    2. CANNED: a `_CONTENT_MATCHERS` row hits the label AND one of its SSOT paths
       resolves -- but ONLY on a field that carries OPTIONS or is short-text
       shaped, NEVER on an essay-shaped one (`is_free_text`: no option list, and
       either a long-text control or an essay-sized `max_length`). The canned
       table matches loose label substrings ("relocat"), and those same
       questions appear BOTH as a two-option dropdown and as an
       800-character essay box ("Are you willing to relocate, and what would help
       you settle in?"). Routing the one-word canned scalar into the essay would
       answer an essay question with "Yes", DISCARD the generated essay written
       for that very field, and still count the field complete. Essay-shaped
       fields take generated answers only; with no generation they stay honestly
       skipped.
    3. GENERATED: an answer whose key equals the field key, else one whose
       normalized label equals the normalized field label.
    4. Nothing: stays skipped, listed in `OverlayReport.unresolved`.

    Steps 2 and 3 are a PREFERENCE, not a commitment: both candidates are
    collected (`_candidates`) and each is fitted in turn (`_first_fitting`), so a
    canned route whose one-word scalar matches none of the field's options, or
    overruns its cap, falls through to the generated answer instead of blocking it
    and costing the field a fill it had. Canned still wins wherever both fit.

    A candidate value for a field WITH options must map onto exactly one option
    label (`_fit_to_options`); otherwise the field stays skipped with the reason
    "no option match" and nothing is guessed. A value that survives that step but
    is longer than the field's own `max_length` (`_overflows`) is not filled
    either: the form would truncate it mid-sentence, so the field stays skipped
    with `OVER_MAX_LENGTH` rather than being counted complete with half a
    sentence in it. When several candidates fail, the recorded reason is the last
    failure.

    `posting_lang` is the language the posting is being answered in. Generated
    answers written for a DIFFERENT language are not applied (an English essay
    must never land in an Italian posting); canned SSOT routes are language-
    agnostic and unaffected.

    The overlay supplies VALUES only. It never drives the page and never revisits
    drivability: the checkbox/radio click-hazard policy stays in the vendor fill
    layer, which already hands those off.
    """
    report = OverlayReport()
    by_key: dict[str, Field] = {fld.key: fld for fld in fieldmap.fields}
    still_skipped: list[tuple[str, str]] = []
    # BEFORE the overlay adds anything: `resolved.fields` is exactly what the
    # KERNEL resolved, which is what this scan is about.
    report.misrouted.extend(_misrouted_kernel_dates(resolved, by_key, generated))
    _announce_misrouted(report.misrouted)

    for key, reason in list(resolved.skipped):
        fld = by_key.get(key)
        if fld is None or _is_upload_field(fld) or is_policy_declined(fld):
            still_skipped.append((key, reason))
            continue
        label = _normalize(fld.label)

        if generated is not None and _tos_forbids(generated, label):
            report.tos_forbidden.append(key)
            # RELABEL the skip reason to the class-ii ToS-forbidden verdict so the
            # completeness census (`kernel.resolve._completeness`, which reads the
            # FillReport skips) subtracts it as a documented ToS handoff, not the
            # kernel's stale data-gap reason ("missing:canned_answers.*" / "...
            # resolved to a mapping with no usable scalar"). Without this the
            # overlay knew the verdict but the census never saw it (F-1/F-10).
            still_skipped.append(
                (key, _tos_forbidden_skip_reason(generated, label)))
            continue

        policy = _policy_verdict(fld, label, ssot, generated)
        if policy.owned:
            value, source, why = _first_fitting(
                fld, _owned_candidate(policy), policy.reason)
        else:
            value, source, why = _first_fitting(fld, _candidates(
                fld, label, ssot, generated, posting_lang), reason)
        if value is None:
            still_skipped.append((key, why))
            report.unresolved.append((key, why))
            continue

        resolved.fields.append(FieldValue(
            key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
            value=value, asset=None))
        report.applied.append((key, source))

    resolved.skipped[:] = still_skipped
    # H.2: the how-heard primary now carries its FINAL resolved option (the loop
    # above ran `_how_heard_verdict`), so the paired specify box can be cleared
    # when that primary did not land on Other.
    _clear_specify_when_primary_not_other(resolved, fieldmap)
    return report


MISROUTE_WARNING = "[content] KERNEL MISROUTE"


def _announce_misrouted(misrouted: list[tuple[str, str]]) -> None:
    """Say out loud, on stderr, every kernel misroute the scan found.

    The REPORT half of "detect and report". `OverlayReport.misrouted` alone reaches
    nobody today: the overlay's only caller is frozen and drops the list, so a
    defect that types prose into a real employer's date box would be detected,
    recorded in an object nothing reads, and thrown away with it. A stderr line
    lands in the run's own output, which is where a human and the acceptance gate
    both look, and it costs the frozen caller nothing.

    STDERR by construction: a caller that prints a JSON result document to stdout
    (`w5_accept.py`) must not have it corrupted by a warning.
    """
    for key, reason in misrouted:
        print(f"{MISROUTE_WARNING} key={key}: {reason}. Left exactly as the kernel "
              f"wrote it (the overlay is additive only and the kernel is frozen); "
              f"this field needs a human.", file=sys.stderr)


# -- resolution ladder helpers ------------------------------------------------

def _tos_forbids(generated: GeneratedAnswers, label: str) -> bool:
    return any(_normalize(entry.label) == label and label
               for entry in generated.tos_forbidden)


def _tos_forbidden_skip_reason(generated: GeneratedAnswers, label: str) -> str:
    """The class-ii skip reason for a ToS-forbidden field: the shared prefix
    (`TOS_FORBIDDEN_SKIP_PREFIX`, which the census keys on) plus the specific
    per-entry reason the generator recorded (e.g. "employer forbids AI-generated
    content", or the AI-policy attestation handoff note), so a human reading the
    census sees WHY the field is a documented handoff and not merely that it is."""
    entry_reason = next(
        (entry.reason for entry in generated.tos_forbidden
         if _normalize(entry.label) == label and label), "")
    return (f"{TOS_FORBIDDEN_SKIP_PREFIX}: {entry_reason}" if entry_reason
            else TOS_FORBIDDEN_SKIP_PREFIX)


def _owned_candidate(policy: PolicyVerdict) -> list[tuple[str, str]]:
    """The ONLY value a policy-OWNED field may ever be offered: the policy's own,
    and an EMPTY list when the policy declined to supply one.

    The whole fail-closed guarantee of the policy tier lives in this list being
    SHORT. `_first_fitting` walks candidates in turn and a candidate that does not
    fit does not block the ones behind it -- so anything appended here behind the
    policy value would become the answer to a question the policy refused, or to
    one whose own control rejected the policy's answer. There is nothing behind
    it: an owned field either takes its policy value or stays EMPTY and is
    reported in `OverlayReport.unresolved`. See `PolicyVerdict`."""
    if policy.value is None:
        return []
    return [(policy.value, policy.source)]


def _candidates(fld: Field, label: str, ssot: SSOT,
                generated: GeneratedAnswers | None,
                posting_lang: str) -> list[tuple[str, str]]:
    """Every value that could fill an UNOWNED field, best route FIRST: the canned
    SSOT route (never on an essay-shaped field), then the generated answer.

    A field a policy OWNS never reaches this list at all (`apply_content_overlay`
    routes it to `_owned_candidate`): it takes its policy value or stays empty,
    rather than falling through to a route its own policy just refused. So this
    is the ladder for the fields NO policy claimed, and neither the canned table
    (which matches loose label substrings) nor a generated answer can reach a
    date box, a city box, or a referral box behind their resolvers' backs.

    A LIST, not a first-hit: the ladder's ORDER is the preference, but a canned
    route that turns out not to fit the field must not be the field's last word
    (see `_first_fitting`)."""
    found: list[tuple[str, str]] = []
    if not is_free_text(fld.type, fld.norm_type, fld.max_length, fld.options):
        canned, source = _canned_candidate(label, ssot, fld.options)
        if canned is not None:
            found.append((canned, source))
    if generated is not None:
        answer, source = _generated_candidate(fld, label, generated, posting_lang)
        if answer is not None:
            found.append((answer, source))
    return found


def _first_fitting(fld: Field, candidates: list[tuple[str, str]],
                   pending_reason: str) -> tuple[str | None, str, str]:
    """The first candidate that maps onto a unique option (`_fit_to_options`) AND
    fits the field's own `max_length` (`_overflows`), as (value, source, reason).

    Each candidate is fitted IN TURN, and a candidate that fails does not block the
    ones behind it: the canned table matches loose label substrings, so its one-word
    scalar can hit a field whose options it matches none of ("Yes" against
    "Hybrid"/"Fully remote"), and stopping there would leave the field skipped even
    though the generated answer written for that very field WOULD have fitted --
    coverage lost to a route that was never usable. Preference still wins wherever
    both fit: the order of `candidates` is the ladder.

    Nothing is guessed either way. When NO candidate fits, the reason recorded is
    the LAST failure (the most specific thing a human can act on), or the kernel's
    own `pending_reason` when there was no candidate to fit at all.
    """
    reason = pending_reason
    for candidate, source in candidates:
        value = _fit_to_options(fld, candidate)
        if value is None:
            reason = NO_OPTION_MATCH
            continue
        if _overflows(fld, value):
            reason = OVER_MAX_LENGTH
            continue
        return value, source, ""
    return None, "", reason


def _canned_candidate(label: str, ssot: SSOT,
                      options: list[str] | None = None) -> tuple[str | None, str]:
    """The first canned route whose keywords hit the label and whose SSOT path
    resolves to a usable scalar, as (value, "canned:<path>").

    ROUTE ORDER IS SHAPE-AWARE. A matcher row may carry both an exact-option
    scalar (`*_dropdown`: "Yes") and the owner's nuanced prose ("Location-
    conditional. Home city: up to 5 days/week ..."), because the same subject is
    asked BOTH ways across vendors. Which one leads depends on the CONTROL:

    * an OPTION-bearing control (a Yes/No select) can only take the scalar; the
      prose fits no option and would leave the required field blank (the live
      2026-07-13 anthropic bug). Scalar first.
    * an option-less TEXT box takes ANY string (`_fit_to_options` has no options to
      reject it), so the scalar would win by mere position and answer "What
      in-office attendance are you open to?" with the single word "Yes", throwing
      the owner's real position away. Prose first: the dropdown scalars sink to the
      end of the ladder, where they still serve as a last resort rather than as a
      silent downgrade of a nuanced answer.

    The rule is keyed on the PATH SUFFIX, so a new row inherits it for free: a path
    named `*_dropdown` is by construction an answer to a dropdown.

    AND A BARE YES/NO NEVER REACHES AN OPTION-LESS CONTROL. "Yes" is an answer to a
    BOOLEAN CONTROL, and only a control that OFFERS options is one. The canned table
    matches loose label substrings, so `("relocat",)` also hits the live
    relocation-ADDRESS box -- a free text field asking WHERE the applicant would
    move from -- and both of that row's routes render as the single word "Yes" (the
    SSOT's `willing_to_relocate` is a boolean, and `_scalar_text` renders a boolean
    as "Yes"). A text box takes any string verbatim (`_fit_to_options` has no
    options to reject it), so the word landed in the address box, filled it, and
    counted it complete. The control decides, never the label: a yes/no scalar is
    offered ONLY where a yes/no option exists to carry it, and an option-less box
    takes prose or nothing. Blast radius (stated, not assumed): this is a single
    control-shape rule in the shared canned router, so it applies identically to all
    four vendors and to every row of `_CONTENT_MATCHERS` whose SSOT path renders a
    bare "Yes"/"No" -- relocation, previously-applied/interviewed, in-office. In
    each, a control with options is unaffected (the scalar is exactly what it wants),
    and an option-less one now takes the row's PROSE route or stays honestly skipped.
    """
    for keywords, paths in _CONTENT_MATCHERS:
        if not any(keyword in label for keyword in keywords):
            continue
        ordered = list(paths)
        if not options:
            ordered.sort(key=lambda p: p.endswith("_dropdown"))
        for path in ordered:
            value = ssot.get(path)
            if value is MISSING:
                continue
            text = _scalar_text(value)
            if text is None:
                continue
            if not options and _normalize(text) in _YES_NO_TOKENS:
                continue
            return text, f"canned:{path}"
    return None, ""


def _generated_candidate(fld: Field, label: str, generated: GeneratedAnswers,
                         posting_lang: str) -> tuple[str | None, str]:
    """The generated answer for this field: exact key first, normalized label
    second. Skipped entirely when the generation was written for a different
    posting language."""
    if _normalize(generated.posting_lang) != _normalize(posting_lang):
        return None, ""
    for answer in generated.answers:
        if answer.key and answer.key == fld.key:
            return answer.value, "generated:key"
    for answer in generated.answers:
        if answer.label and label and _normalize(answer.label) == label:
            return answer.value, "generated:label"
    return None, ""


def _fit_to_options(fld: Field, value: str) -> str | None:
    """Map `value` onto one of the field's option labels, or None.

    Three steps, each requiring a UNIQUE hit; zero hits falls through to the next
    step, 2+ hits stops the ladder (ambiguity is never resolved by guessing):

    (a) exact, case-insensitive;
    (b) the value's leading yes/no token, against the option whose own leading
        token is the same yes/no (this is what carries "Yes" onto the verbose
        "Yes, I am willing to relocate");
    (c) token subset either way: the option whose token set is a subset of the
        value's, or whose token set contains the value's.

    A field with NO options takes the value verbatim.
    """
    if not fld.options:
        return value

    lowered = _normalize(value)
    hits = [opt for opt in fld.options if _normalize(opt) == lowered]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return None

    token = _leading_yes_no(value)
    if token is not None:
        hits = [opt for opt in fld.options if _leading_yes_no(opt) == token]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None

    value_tokens = _tokens(value)
    if not value_tokens:
        return None
    hits = [opt for opt in fld.options
            if (opt_tokens := _tokens(opt))
            and (opt_tokens <= value_tokens or value_tokens <= opt_tokens)]
    return hits[0] if len(hits) == 1 else None


def _overflows(fld: Field, value: str) -> bool:
    """True iff `value` is longer than the field's OWN `max_length` (a cap the
    posting declared; a field with no cap has nothing to overflow).

    The last check before a value reaches `ResolvedValues`, and the only one that
    sees CANNED text as well as generated prose: the offline generator refuses an
    over-cap MODEL answer at write time (`bin/generate_answers.py`), but it never
    sees an SSOT canned route, so without this the one class of value nothing
    validated would be the deterministic one.
    """
    limit = int(fld.max_length or 0)
    return bool(limit) and len(value) > limit


def _scalar_text(value) -> str | None:
    """The fill text for an SSOT scalar, or None when the node is a structure
    (a dict/list canned answer is not a single answer and is never flattened)."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _normalize(text) -> str:
    """Casefold, collapse whitespace, drop a required-marker asterisk and edge
    punctuation. The shared comparison key for labels, options, and values."""
    collapsed = _WS_RE.sub(" ", str(text or "").casefold()).strip()
    return collapsed.strip("*").strip(" \t.,:;!?").strip()


def _tokens(text) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_SPLIT_RE.split(_normalize(text)) if t)


def _leading_yes_no(text) -> str | None:
    """The leading yes/no token of a value or an option label, or None.
    "Yes, I am willing to relocate" -> "yes"; "Fully remote" -> None."""
    normalized = _normalize(text).replace("ì", "i")
    head = _TOKEN_SPLIT_RE.split(normalized)[0] if normalized else ""
    return head if head in _YES_NO_TOKENS else None
