"""SSOT-grounded content channel: canned routing, option matching, essay delivery.

Sibling of `engine.draft`: it lives OUTSIDE `engine/kernel/` (the kernel is frozen)
and OUTSIDE `engine/providers/<vendor>/` (all four vendor plugins may import it
without breaching their pairwise import-disjointness).

Two jobs, both DETERMINISTIC and OFFLINE:

1. ROUTE the canned answers the kernel resolver leaves behind. The kernel's
   `_ANSWER_MATCHERS` is hash-adjacent frozen surface; this module carries its OWN
   `_CONTENT_MATCHERS` table for the question families the kernel never routed
   ("how did you hear about us", in-office attendance, previously applied /
   interviewed, the verbose relocation dropdown).
2. DELIVER pre-generated free-text answers (essays, cover letters, any
   write-your-answer field) that `bin/generate_answers.py` produced OFFLINE, on
   toto, from the SSOT. Generation is a separate tool the engine NEVER imports.

STATUS: staged plumbing. Nothing in the engine calls `apply_content_overlay` yet;
the vendor loops and the acceptance gate are wired to it in a later stage. Every
statement below about what the gate or a vendor loop DOES with the overlay's
report describes the intended contract of that wiring, not code that runs today.

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

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from engine.kernel.contracts import Field, FieldMap, FieldValue, ResolvedValues
from engine.kernel.fill_toolkit import _is_upload_field
from engine.kernel.resolve import _DECLINE_SECTIONS
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
class GeneratedAnswers:
    vendor: str
    slug: str
    job_id: str
    posting_lang: str
    answers: list[GeneratedAnswer] = field(default_factory=list)
    tos_forbidden: list[TosForbidden] = field(default_factory=list)


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
        tos_forbidden=_parse_forbidden(raw.get("tos_forbidden") or []))


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


# -- canned routing table -----------------------------------------------------

# Substring keywords matched against the NORMALIZED field label, kernel-matcher
# style: the first row with any substring hit wins, and the row applies only when
# one of its candidate SSOT paths resolves (the kernel MISSING sentinel is
# respected). Order is load-bearing. Each row carries a TUPLE of candidate paths
# so the verbose relocation dropdown can fall back to the plain relocation string
# when the dedicated dropdown answer was never seeded.
_CONTENT_MATCHERS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("how did you hear", "how you heard", "where did you hear",
      "how did you find"),
     ("canned_answers.how_did_you_hear_default",)),
    (("in-person", "in person", "in office", "in the office", "on-site",
      "onsite"),
     ("canned_answers.in_office_attendance",)),
    # Deliberately NOT keyed on the bare "applied to": that substring also carries
    # "Which team have you applied to?", a SHORT-TEXT question whose answer is a
    # team name, and the canned route would fill it with the previously-applied
    # "No". The keywords below all name the have-you-applied-BEFORE question.
    (("interviewed", "previously applied", "applied before"),
     ("canned_answers.previously_applied_default",)),
    (("relocat",),
     ("canned_answers.relocation_dropdown",
      "canned_answers.willing_to_relocate")),
]


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
    """
    applied: list[tuple[str, str]] = field(default_factory=list)
    tos_forbidden: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)


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
    2. CANNED: a `_CONTENT_MATCHERS` row hits the label AND one of its SSOT paths
       resolves -- but ONLY on a field that carries OPTIONS or is short-text
       shaped, NEVER on an essay-shaped one (`is_free_text`: no option list, and
       either a long-text control or an essay-sized `max_length`). The canned
       table matches loose label substrings ("relocat", "how did you hear"), and
       those same questions appear BOTH as a two-option dropdown and as an
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

    for key, reason in list(resolved.skipped):
        fld = by_key.get(key)
        if fld is None or _is_upload_field(fld) or is_policy_declined(fld):
            still_skipped.append((key, reason))
            continue
        label = _normalize(fld.label)

        if generated is not None and _tos_forbids(generated, label):
            report.tos_forbidden.append(key)
            still_skipped.append((key, reason))
            continue

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
    return report


# -- resolution ladder helpers ------------------------------------------------

def _tos_forbids(generated: GeneratedAnswers, label: str) -> bool:
    return any(_normalize(entry.label) == label and label
               for entry in generated.tos_forbidden)


def _candidates(fld: Field, label: str, ssot: SSOT,
                generated: GeneratedAnswers | None,
                posting_lang: str) -> list[tuple[str, str]]:
    """Every value that could fill this field, best route FIRST: the canned SSOT
    route (never on an essay-shaped field), then the generated answer.

    A LIST, not a first-hit: the ladder's ORDER is the preference, but a canned
    route that turns out not to fit the field must not be the field's last word
    (see `_first_fitting`)."""
    found: list[tuple[str, str]] = []
    if not is_free_text(fld.type, fld.norm_type, fld.max_length, fld.options):
        canned, source = _canned_candidate(label, ssot)
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


def _canned_candidate(label: str, ssot: SSOT) -> tuple[str | None, str]:
    """The first canned route whose keywords hit the label and whose SSOT path
    resolves to a usable scalar, as (value, "canned:<path>")."""
    for keywords, paths in _CONTENT_MATCHERS:
        if not any(keyword in label for keyword in keywords):
            continue
        for path in paths:
            value = ssot.get(path)
            if value is MISSING:
                continue
            text = _scalar_text(value)
            if text is not None:
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
