"""Vendor-agnostic resolve engine: coverage classification + fill-value render.

The kernel's deterministic (no-LLM) decision core, moved verbatim from
`engine.fieldmap` (the generic `coverage`/`_classify_field` cluster) and
`engine.fill` (the `resolve_values` closure). Two responsibilities:

1. COVERAGE -- classify every required field as answerable / missing:<path-guess>
   / manual-only, by keyword-matching the SSOT buckets (`coverage`,
   `_classify_field`, `_answerable_path`, `_manual_only_reason`).
2. RESOLVE -- render each field to a concrete fill value by type (`resolve_values`
   and its render/select/boolean/upload helpers), and compute a fill report's
   completeness denominator (`_completeness`).

VENDOR-WIDGET INJECTION SEAM (W5.1 spec 3.4): the kernel carries NO vendor
portal-widget knowledge. Greenhouse's location-autocomplete, paste-in
resume/cover-letter textareas, and longitude/latitude telemetry are reconnected
through a duck-typed `vendor_resolver` (methods `location_path`, `key_text_path`,
`manual_reason`, `hidden_widget`). The default `_NOOP_RESOLVER` is a vendor with
no quirks; `engine.fieldmap.GREENHOUSE_WIDGET_RESOLVER` supplies the Greenhouse
behaviour and is injected by the `engine.fieldmap.coverage` /
`engine.fill.resolve_values` / `engine.fill._completeness` shims. Stage 2
relocates the adapter to the greenhouse plugin; Stage 3 moves callers onto the
kernel + registry-built injection and dissolves those shims.

Layering: imports only stdlib + `engine.kernel.*`.
Nothing from `engine.fieldmap` / `engine.fill` / `engine.providers` / pipeline
ever enters here -- enforced by the kernel-layering invariant test with NO
allowlist entry for this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.kernel.contracts import (
    Field,
    FieldMap,
    FieldValue,
    FillAssets,
    ResolvedValues,
    Section,
)
from engine.kernel.fill_toolkit import _is_upload_field
from engine.kernel.ssot import MISSING, SSOT


# -- vendor-widget injection seam (spec 3.4) -----------------------------------

class _NoopVendorResolver:
    """Default vendor_resolver: a vendor with no portal-widget quirks."""
    def location_path(self, fld, ssot): return None
    def key_text_path(self, fld, ssot): return None
    def manual_reason(self, fld): return ""
    def hidden_widget(self, fld): return False


_NOOP_RESOLVER = _NoopVendorResolver()


# ============================================================================ #
# Generic coverage classification (moved from engine.fieldmap).
# ============================================================================ #

# The three classification verdicts a required field can receive.
ANSWERABLE = "answerable"
MISSING_STATUS = "missing"
MANUAL_ONLY = "manual-only"

# Sections that are always declinable and never block a fill/coverage run.
_DECLINE_SECTIONS = frozenset({
    Section.COMPLIANCE_EEOC, Section.DEMOGRAPHIC, Section.VOLUNTARY,
})

# Label keywords that mark a field as EEO/demographic no matter which section it
# arrived in (defence in depth on top of the source tag).
_DEMOGRAPHIC_KEYWORDS = (
    "gender", "race", "ethnic", "veteran", "disability", "disabilities",
    "sexual orientation", "hispanic", "latino", "self-identification",
    "self identification",
)

# A required "how much X experience do you have" question is answerable
# in principle: the SSOT's skills bucket can decide yes/no for any named
# technology, even if the honest answer is "no" (answerability is about
# whether the SSOT can decide, not about the polarity of the answer).
_SKILLS_EXPERIENCE_RE = re.compile(r"experience\s+(?:using|with|in)\b",
                                  re.IGNORECASE)

# First/last name label keywords, shared with `engine.fill` (imported there) so
# the full-name-split fallback at render time detects the same fields these
# matchers do.
_FIRST_NAME_KEYWORDS = ("first name", "given name", "forename")
_LAST_NAME_KEYWORDS = ("last name", "surname", "family name")

# Ordered label-keyword -> candidate SSOT dotted paths. First matcher whose any
# keyword is a substring of the (lowercased) label wins; within it the first
# candidate path that resolves in the SSOT makes the field answerable. The
# discrete first_name/last_name key leads each list so a form with BOTH a First
# Name and a Last Name field never has the full name typed into both; the
# full-name paths remain as a fallback (split at render time in engine.fill)
# for an SSOT that only carries a combined name. Order is load-bearing: the
# country-of-residence matcher MUST precede the generic current-location
# matcher (below) so a "country of residence" question resolves the discrete
# `identity.country` rather than the full postal address, which matches no
# country-name option.
_ANSWER_MATCHERS: list[tuple[tuple[str, ...], list[str]]] = [
    (_FIRST_NAME_KEYWORDS,
     ["identity.first_name", "identity.name", "identity.full_name"]),
    (_LAST_NAME_KEYWORDS,
     ["identity.last_name", "identity.name", "identity.full_name"]),
    (("full name", "legal name", "your name"),
     ["identity.name", "identity.full_name"]),
    (("email", "e-mail"), ["identity.email"]),
    (("phone", "mobile number", "telephone"),
     ["identity.phone", "canned_answers.phone"]),
    (("linkedin",), ["links.linkedin", "canned_answers.linkedin"]),
    (("github",), ["links.github"]),
    (("portfolio", "personal website", "personal site", "web site", "website"),
     ["links.site", "links.website", "links.portfolio"]),
    (("notice period", "notice",),
     ["canned_answers.notice_period"]),
    (("employment agreement", "post-employment", "post employment",
      "non-compete", "noncompete", "restrictive covenant"),
     ["canned_answers.post_employment_restrictions"]),
    (("previously worked", "previously consulted", "worked at or consulted",
      "previously employed at", "previously interned"),
     ["canned_answers.previously_worked_at_company",
      "canned_answers.previously_applied_default"]),
    (("sponsorship", "sponsor", "visa"),
     ["canned_answers.sponsorship_answer_by_region",
      "canned_answers.visa_sponsorship_required",
      "canned_answers.us_visa_sponsorship_required"]),
    (("authorized to work", "authorised to work", "right to work",
      "eligible to work", "work authorization", "work authorisation",
      "legally authorized", "legally authorised", "work permit"),
     ["work_authorization", "canned_answers.work_authorization"]),
    (("relocat",),
     ["canned_answers.relocation", "canned_answers.willing_to_relocate"]),
    (("salary", "compensation expectation", "expected", "desired compensation"),
     ["preferences.comp_floor", "canned_answers.salary_expectation"]),
    (("country of residence", "current country", "country you reside",
      "country you are located"),
     ["identity.country"]),
    (("currently located in",
      "where are you currently located", "where are you located",
      "current location", "location"),
     ["identity.current_location", "identity.address", "identity.country"]),
    (("please confirm", "privacy policy", "consent to", "i agree"),
     ["canned_answers.optional_consents"]),
    (("accommodation", "accommodations", "accessible and inclusive",
      "reasonable adjustment", "accessibility need"),
     ["canned_answers.accommodations"]),
    (("name",), ["identity.name", "identity.full_name"]),
]


@dataclass
class FieldCoverage:
    key: str
    label: str
    status: str          # answerable | missing | manual-only
    path: str            # answerable: resolving path; missing: dotted-path guess
    reason: str = ""     # manual-only: why (file-upload | demographic/EEO)

    def classification(self) -> str:
        """The compact verdict string (`missing:` carries the guessed path)."""
        if self.status == MISSING_STATUS:
            return f"{MISSING_STATUS}:{self.path}"
        return self.status


@dataclass
class CoverageReport:
    vendor: str
    posting_id: str
    fields: list[FieldCoverage]

    @property
    def answerable(self) -> int:
        return sum(1 for f in self.fields if f.status == ANSWERABLE)

    @property
    def missing(self) -> int:
        return sum(1 for f in self.fields if f.status == MISSING_STATUS)

    @property
    def manual_only(self) -> int:
        return sum(1 for f in self.fields if f.status == MANUAL_ONLY)

    @property
    def required_total(self) -> int:
        return len(self.fields)

    def missing_paths(self) -> list[str]:
        """Dotted-path guesses for every unanswerable required field (feeds 7.6)."""
        return [f.path for f in self.fields if f.status == MISSING_STATUS]

    def summary_line(self) -> str:
        return (f"{self.answerable} answerable, {self.missing} missing, "
                f"{self.manual_only} manual-only of {self.required_total} required")


def coverage(fieldmap: FieldMap, ssot: SSOT, profile: dict,
             vendor_resolver=None) -> CoverageReport:
    """Classify every REQUIRED field of `fieldmap` against the SSOT + profile.

    Deterministic, no LLM. Order per field: manual-only (file upload or
    EEO/demographic, never auto-answered) wins first; then a keyword match
    against the SSOT buckets makes it answerable; otherwise it is missing and
    gets a dotted-path guess (canned_answers.<slug> for an unrecognised
    question) that a questionnaire item can later resolve.

    `vendor_resolver` (spec 3.4) reconnects any vendor portal-widget quirks; it
    is resolved to `_NOOP_RESOLVER` (no quirks) once and threaded down. The
    Greenhouse behaviour is injected by `engine.fieldmap.coverage`.
    """
    resolver = vendor_resolver if vendor_resolver is not None else _NOOP_RESOLVER
    profile = profile or {}
    results: list[FieldCoverage] = []
    for fld in fieldmap.required_fields():
        results.append(_classify_field(fld, ssot, profile, resolver))
    return CoverageReport(vendor=fieldmap.vendor,
                          posting_id=fieldmap.posting_id, fields=results)


def _classify_field(fld: Field, ssot: SSOT, profile: dict,
                    vendor_resolver=_NOOP_RESOLVER) -> FieldCoverage:
    reason = _manual_only_reason(fld, vendor_resolver)
    if reason:
        return FieldCoverage(fld.key, fld.label, MANUAL_ONLY, "", reason)
    path = _answerable_path(fld, ssot, profile, vendor_resolver)
    if path is not None:
        return FieldCoverage(fld.key, fld.label, ANSWERABLE, path)
    return FieldCoverage(fld.key, fld.label, MISSING_STATUS,
                         _missing_path_guess(fld.label))


def _manual_only_reason(fld: Field, vendor_resolver=_NOOP_RESOLVER) -> str:
    """"file-upload" ONLY for a genuine file control: a native file type, or a
    label carrying an explicit upload/attach verb (mirrors `engine.fill`'s
    `_is_upload_field`). A bare "resume"/"cv" label keyword is NOT enough --
    Greenhouse's paste-in `resume_text`/`cover_letter_text` textareas share
    their label with the sibling file-upload field ("Resume"/"Resume/CV"),
    so tagging on the label alone would wrongly classify a fillable free-text
    field as manual-only file-upload (never resolved, never fillable).

    A vendor portal-widget manual reason (e.g. Greenhouse's longitude/latitude
    "portal-widget") is asked LAST, via the injected `vendor_resolver`."""
    if "file" in fld.type.lower():
        return "file-upload"
    label = fld.label.lower()
    if any(word in label for word in ("upload", "attach")):
        return "file-upload"
    if fld.source in ("demographic", "eeo", "eeoc", "compliance"):
        return "demographic/EEO"
    if any(word in label for word in _DEMOGRAPHIC_KEYWORDS):
        return "demographic/EEO"
    reason = vendor_resolver.manual_reason(fld)
    if reason:
        return reason
    return ""


def _answerable_path(fld: Field, ssot: SSOT, profile: dict,
                     vendor_resolver=_NOOP_RESOLVER) -> str | None:
    location_path = vendor_resolver.location_path(fld, ssot)
    if location_path is not None:
        return location_path
    key_text_path = vendor_resolver.key_text_path(fld, ssot)
    if key_text_path is not None:
        return key_text_path
    low = fld.label.lower()
    if _SKILLS_EXPERIENCE_RE.search(low) and ssot.get("skills") is not MISSING:
        return "skills"
    for keywords, candidates in _ANSWER_MATCHERS:
        if not any(keyword in low for keyword in keywords):
            continue
        for path in candidates:
            if ssot.get(path) is not MISSING:
                return path
        if _profile_answers_work_auth(candidates, profile):
            return "profile.capabilities"
        return None
    return None


def _profile_answers_work_auth(candidates: list[str], profile: dict) -> bool:
    """Work-authorization questions may be answered from a profile capability
    (e.g. work_authorization_eu) even when the raw SSOT string is absent."""
    if "work_authorization" not in candidates:
        return False
    caps = profile.get("capabilities") or []
    return any(str(cap).startswith("work_authorization") for cap in caps)


def _missing_path_guess(label: str) -> str:
    """A free-form unrecognised question is answered from canned_answers (7.6)."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    slug = slug or "unlabelled"
    return f"canned_answers.{slug}"


# ============================================================================ #
# Deterministic value resolution (moved from engine.fill).
# ============================================================================ #

# Canned-answer paths (checked in order) that ratify consent: the first that
# resolves to a non-negative value gates every consent/confirmation checkbox to
# True. The real SSOT keys this as `privacy_consent_default`; the synthetic v1.4
# fixture keys it as `optional_consents`, so both are consulted.
_CONSENT_SOURCE_PATHS = (
    "canned_answers.privacy_consent_default",
    "canned_answers.optional_consents",
)

# Field types that render as an option choice rather than free text.
_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no",
})

# -- checkbox intent classifiers (criterion: consent checkboxes) ---------------
# A checkbox is ticked True only when its label reads as a legal
# consent/confirmation ask, or as a talent-pool / future-opportunities opt-in
# (YES per the owner split). A pure marketing/newsletter box is left unticked.
# Order at the call site is talent-pool -> marketing -> consent, so a "marketing"
# ask that also says "I agree" is never mistaken for legal consent.
_CONSENT_RE = re.compile(
    r"please confirm|privacy|consent|i agree|\bagree\b|\bterms\b|gdpr|"
    r"data processing|i acknowledge|i certify|i confirm", re.I)
_TALENT_POOL_RE = re.compile(
    r"talent (pool|community|network)|future opportunit|future role|"
    r"keep .*on file|consider me for|stay in touch|keep me in mind|"
    r"other (roles|positions|opportunit)", re.I)
_MARKETING_RE = re.compile(
    r"marketing|newsletter|promotional|promotions|subscribe|mailing list|"
    r"updates and offers|product updates|latest news", re.I)

# -- yes/no select intent + region coverage (criterion: yes/no selects) --------
# Right-to-work / sponsorship selects are answered by deriving an affirmative or
# negative from the SSOT work-authorization facts, then picking the matching
# Yes/No option. A posting whose label targets a region the SSOT does not cover
# (e.g. the United States) is region-ambiguous and is left honestly unfilled.
_SPONSOR_INTENT_RE = re.compile(r"sponsor|\bvisa\b", re.I)
_WORK_AUTH_INTENT_RE = re.compile(
    r"authori[sz]ed to work|authori[sz]ation to work|right to work|"
    r"eligible to work|legally (authori[sz]ed|entitled|permitted|able)|"
    r"work permit|work authori[sz]ation|permitted to work|able to work in|"
    r"do you have the right to work", re.I)
_COVERED_REGION_RE = re.compile(
    r"\beu\b|\be\.u\.\b|european union|\beurope\b|\beea\b|ital", re.I)
_UNCOVERED_REGION_RE = re.compile(
    r"united states|\bu\.?s\.?a?\.?\b|\bamerica|\bcanad|"
    r"united kingdom|\bu\.?k\.?\b|\bbritain\b|\bengland\b|\baustralia\b|"
    r"\bindia\b|\bsingapore\b|\buae\b|\bdubai\b", re.I)
_YESNO_NEG_RE = re.compile(r"^\s*(no\b|n\b|not\b|none\b|false\b|nope\b)", re.I)
_YESNO_POS_RE = re.compile(r"^\s*(yes\b|y\b|true\b|yep\b|yeah\b)", re.I)

# A candidate-photo control: label reads like a portrait ask (English + Italian)
# or the field is an image-accepting file input (criterion 3).
_PHOTO_LABEL_RE = re.compile(
    r"photo|picture|headshot|profile image|foto|immagine", re.I)

# A cover-letter file-upload control: key or label names "cover letter" (also
# matches the underscore/hyphen key form cover_letter, cover-letter-text). A
# match here resolves to the dedicated `FillAssets.cover_letter` document
# asset when one is present, and must NEVER fall through to the CV-selection
# branch -- see the live-run bug where a `cover_letter` file field silently
# received `cv-ats.pdf`. With no cover-letter asset it is honestly skipped
# (there is nothing to upload). Deliberately narrow (requires "cover"
# immediately followed by "letter") so it never misfires on `resume` or
# `avatar`/`photo` keys that merely share a stray token.
_COVER_LETTER_RE = re.compile(r"cover[\s_-]*letter", re.I)

# Posting-language tokens recognised as Italian. Retained only for the
# `_is_italian` helper (re-exported through `engine.fill`); the CV/photo choice
# no longer consults posting language -- see `_select_cv`, the owner-ratified
# structural rule (2026-07-07).
_ITALIAN_LANGS = frozenset({"it", "it-it", "italian", "italiano"})


def resolve_values(fieldmap: FieldMap, ssot: SSOT, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en",
                   vendor_resolver=None) -> ResolvedValues:
    """Classify + render every field of `fieldmap` into concrete fill values.

    File-upload fields resolve to a whitelisted asset (owner override): a
    candidate-photo field gets the profile photo, a cover-letter file field
    (key/label matching "cover letter", e.g. `cover_letter`) gets the
    dedicated cover-letter document asset when one is present in `FillAssets`
    -- it must NEVER receive the CV instead -- and is otherwise honestly
    SKIPPED (no cover-letter document asset), and every OTHER file field gets
    a CV picked by the owner-ratified structural rule (cv-ats when the form has
    a photo field, so the photo attaches separately; cv-atsi when it has none,
    so the ATSI variant embeds the photo -- posting-language independent). With
    no `assets` (the pre-override default) file fields keep the old
    "file-upload" skip, so the existing contract holds.

    A checkbox (boolean) is resolved by its label intent (`_resolve_boolean`): a
    consent/confirmation box ticks True when the SSOT ratifies consent, a
    talent-pool box ticks True, a marketing box is left unticked. Every other
    field reuses `_classify_field` (the SSOT coverage classifier):
    manual-only (EEO-demographic / portal widget) and missing (unanswerable)
    fields are SKIPPED with their classifier reason. An answerable field is
    rendered by type: free text from the resolved SSOT string, and an option
    label for a select (an exact case-insensitive option match, else a yes/no
    normalization for right-to-work / sponsorship questions, else skipped).
    Deterministic, no LLM; never writes the SSOT.

    `vendor_resolver` (spec 3.4) reconnects any vendor portal-widget quirks
    through `_classify_field`; it defaults to `_NOOP_RESOLVER`. The Greenhouse
    behaviour is injected by the `engine.fill.resolve_values` shim.
    """
    resolver = vendor_resolver if vendor_resolver is not None else _NOOP_RESOLVER
    profile = profile or {}
    assets = assets.verified() if assets is not None else None
    resolved = ResolvedValues()
    has_photo_field = _form_has_photo_field(fieldmap)
    for fld in fieldmap.fields:
        if _is_upload_field(fld):
            _resolve_upload(fld, resolved, assets, posting_lang, has_photo_field)
            continue
        if (fld.type or "").lower() == "boolean":
            _resolve_boolean(fld, resolved, ssot, profile, resolver)
            continue
        classified = _classify_field(fld, ssot, profile, resolver)
        if classified.status == MANUAL_ONLY:
            resolved.skipped.append((fld.key, classified.reason or MANUAL_ONLY))
            continue
        if classified.status == MISSING_STATUS:
            resolved.skipped.append((fld.key, classified.classification()))
            continue
        value, skip_reason = _render_value(fld, classified.path, ssot)
        if skip_reason is not None:
            resolved.skipped.append((fld.key, skip_reason))
            continue
        resolved.fields.append(FieldValue(
            key=fld.key, label=fld.label, type=fld.type,
            locator=fld.locator, value=value))
    return resolved


def _is_photo_field(fld) -> bool:
    """A candidate-image field: label matches the portrait pattern (EN + IT)
    (criterion 3). Only consulted for fields that are already upload fields,
    so a stray text match cannot trigger an upload.

    `Field` (engine.fieldmap) carries no `accept` MIME attribute, so an
    accept-sniffing branch would be dead in production; the label regex is the
    sole detection signal."""
    return bool(_PHOTO_LABEL_RE.search(fld.label or ""))


def _form_has_photo_field(fieldmap: FieldMap) -> bool:
    return any(_is_upload_field(f) and _is_photo_field(f) for f in fieldmap.fields)


def _is_cover_letter_field(fld) -> bool:
    """A cover-letter file-upload field: `_COVER_LETTER_RE` matches the key or
    the label. Only consulted for fields already classified as upload fields,
    so a stray text match elsewhere can never trigger this."""
    return bool(_COVER_LETTER_RE.search(fld.key or "")
                or _COVER_LETTER_RE.search(fld.label or ""))


# The cover-letter file field is skipped with this reason ONLY when
# `FillAssets.cover_letter` is absent: the field is optional and there is
# nothing to upload, so it is honestly skipped rather than silently
# receiving the CV (the live-confirmed bug this guards against). When a real
# cover-letter document asset IS present, `_resolve_upload` uploads it
# instead -- see the cover-letter branch below.
_COVER_LETTER_SKIP_REASON = (
    "optional cover-letter upload; no cover-letter document asset (cover "
    "letter is drafted per-posting in the manual flow)")


def _resolve_upload(fld, resolved: ResolvedValues, assets: FillAssets | None,
                    posting_lang: str, has_photo_field: bool) -> None:
    if assets is None:
        # Pre-override contract: no assets -> file fields are skipped, not filled.
        resolved.skipped.append((fld.key, "file-upload"))
        return
    if _is_photo_field(fld):
        asset_name, path, reason = ("photo", assets.photo,
                                    "candidate photo/portrait field")
    elif _is_cover_letter_field(fld):
        if assets.cover_letter is None:
            # Never resolve a cover-letter file field to the CV asset: with
            # no cover-letter document asset there is nothing to upload, and
            # the field is optional, so it is honestly skipped.
            resolved.skipped.append((fld.key, _COVER_LETTER_SKIP_REASON))
            return
        asset_name, path, reason = ("cover-letter", assets.cover_letter,
                                    "cover-letter document asset")
    else:
        asset_name, path, reason = _select_cv(assets, has_photo_field)
    if path is None:
        resolved.skipped.append((fld.key, f"asset missing: {asset_name}"))
        return
    resolved.fields.append(FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=path, asset=asset_name, upload_reason=reason))


def _select_cv(assets: FillAssets, has_photo_field: bool):
    """The owner-ratified structural CV rule (2026-07-07): purely form-driven,
    posting-language INDEPENDENT. A form that HAS a dedicated photo/portrait
    field carries the real photo on that field, so the plain ATS CV is uploaded
    (cv-ats); a form with NO photo field has nowhere to carry the portrait, so
    the embedded-photo ATSI CV variant is uploaded (cv-atsi)."""
    if not has_photo_field:
        return ("cv-atsi", assets.cv_atsi,
                "no photo field on the form; embedding the photo via the ATSI "
                "CV variant")
    return ("cv-ats", assets.cv_ats,
            "photo field present; plain ATS CV, photo attached to the photo "
            "field")


def _is_italian(posting_lang: str) -> bool:
    return str(posting_lang or "").strip().lower() in _ITALIAN_LANGS


# -- checkbox (boolean) resolution ---------------------------------------------

def _resolve_boolean(fld, resolved: ResolvedValues, ssot: SSOT,
                     profile: dict, vendor_resolver=_NOOP_RESOLVER) -> None:
    """Resolve a checkbox by its label intent (criterion: consent checkboxes).

    An EEO/demographic or file boolean stays manual-only (never auto-answered).
    A consent/confirmation box is ticked True when the SSOT ratifies consent; a
    talent-pool / future-opportunities box is ticked True (YES per the owner
    split); a marketing/newsletter box is left unticked; any other checkbox is
    left for a human (unchanged pre-existing behaviour)."""
    classified = _classify_field(fld, ssot, profile, vendor_resolver)
    if classified.status == MANUAL_ONLY:
        resolved.skipped.append((fld.key, classified.reason or MANUAL_ONLY))
        return
    kind = _classify_checkbox(fld.label)
    if kind == "marketing":
        resolved.skipped.append(
            (fld.key, "marketing/newsletter checkbox left unticked"))
        return
    if kind == "talent_pool":
        resolved.fields.append(_bool_field(fld, True))
        return
    if kind == "consent":
        if _consent_ratified(ssot):
            resolved.fields.append(_bool_field(fld, True))
        else:
            resolved.skipped.append(
                (fld.key, "consent checkbox not auto-ticked: SSOT carries no "
                 "ratified consent answer"))
        return
    resolved.skipped.append(
        (fld.key, "non-consent checkbox not auto-checked in dry run"))


def _bool_field(fld, value: bool) -> FieldValue:
    return FieldValue(key=fld.key, label=fld.label, type=fld.type,
                      locator=fld.locator, value=value)


def _classify_checkbox(label: str) -> str | None:
    """One of "talent_pool" | "marketing" | "consent" | None for a checkbox.

    Talent-pool is checked first, then marketing, then consent: a marketing box
    that also says "I agree" must never read as legal consent, and a
    future-opportunities box (owner: YES) must not be dropped as marketing."""
    low = (label or "").lower()
    if _TALENT_POOL_RE.search(low):
        return "talent_pool"
    if _MARKETING_RE.search(low):
        return "marketing"
    if _CONSENT_RE.search(low):
        return "consent"
    return None


def _consent_ratified(ssot: SSOT) -> bool:
    """True iff the SSOT carries a non-negative consent answer (never fabricated:
    an explicit "no" or an absent answer leaves the box unticked)."""
    for path in _CONSENT_SOURCE_PATHS:
        value = ssot.get(path)
        if value is MISSING:
            continue
        if _yesno(value) is not False:   # True or non-yes/no prose -> ratified
            return True
    return False


# Full-name SSOT paths: when the fieldmap matcher falls back to one of these
# (no discrete identity.first_name/identity.last_name in the SSOT), a first- or
# last-name field must split the combined value rather than type it whole into
# both fields.
_FULL_NAME_PATHS = frozenset({"identity.name", "identity.full_name"})


def _render_value(fld, path: str, ssot: SSOT):
    """Render one ANSWERABLE field to (value, None) or (None, skip_reason).

    File and boolean fields are handled by their own branches of `resolve_values`
    and never reach here; the file guard below is defence in depth so a file
    field can never be rendered as free text even if the dispatch changes."""
    if fld.type == "input_file":
        return None, "file-upload"
    raw = ssot.get(path)
    if raw is MISSING:
        return None, f"answerable via {path} but no literal SSOT value"
    if isinstance(raw, dict):
        return _render_dict_value(fld, path, raw)
    if path in _FULL_NAME_PATHS:
        kind = _name_part_kind(fld.label)
        if kind is not None:
            return _split_full_name(kind, path, raw)
    if fld.type in _SELECT_TYPES:
        return _render_select(fld, raw, ssot)
    return _render_text(raw, path)


def _render_dict_value(fld, path: str, raw: dict):
    """A dotted path that resolved to an SSOT sub-tree (dict) rather than a
    scalar. A select field may still be answerable from one of the dict's
    scalar values matching an option (exact match first, then the leading-
    Yes/No-token fallback, `_extract_yesno_option` -- e.g. a region-keyed
    `sponsorship_answer_by_region` dict whose EU sub-value is a full sentence
    "No, I have the right to work..." maps onto a bare "No" option); a text
    field (or a select with no matching scalar) is honestly skipped rather
    than typing/matching the mapping itself."""
    if fld.type in _SELECT_TYPES:
        for value in raw.values():
            match = _match_option(fld.options, value)
            if match is not None:
                return match, None
        for value in raw.values():
            extracted = _extract_yesno_option(fld.options, value)
            if extracted is not None:
                return extracted, None
    return None, f"{path} resolved to a mapping with no usable scalar"


def _name_part_kind(label: str) -> str | None:
    """"first" / "last" / None, using the SAME label keywords the fieldmap
    matchers use to identify a first- or last-name question."""
    low = (label or "").lower()
    if any(keyword in low for keyword in _FIRST_NAME_KEYWORDS):
        return "first"
    if any(keyword in low for keyword in _LAST_NAME_KEYWORDS):
        return "last"
    return None


def _split_full_name(kind: str, path: str, raw):
    """Split a combined-name SSOT value for a discrete first/last name field.

    A single-token name gives the first-name field the whole token; the
    last-name field has nothing left to split out, so it is honestly skipped
    rather than typed as an empty string."""
    tokens = str(raw).split()
    if not tokens:
        return None, _empty_value_skip(path)
    if kind == "first":
        return tokens[0], None
    if len(tokens) == 1:
        return None, f"{path} is a single-token name; no last name to split out"
    return tokens[-1], None


def _render_select(fld, raw, ssot: SSOT):
    if fld.type == "multi_value_multi_select":
        candidates = raw if isinstance(raw, list) else [raw]
        matched = [m for m in (_match_option(fld.options, c) for c in candidates)
                   if m is not None]
        if not matched:
            return None, f"no option matches SSOT value {_short(raw)!r}"
        return matched, None
    intent = _select_intent(fld.label)
    if intent is not None:
        return _resolve_yes_no_select(fld, ssot, intent, raw)
    match = _match_option(fld.options, raw)
    if match is not None:
        return match, None
    extracted = _extract_yesno_option(fld.options, raw)
    if extracted is not None:
        return extracted, None
    return None, f"no option matches SSOT value {_short(raw)!r}"


def _extract_yesno_option(options, raw):
    """Fallback for a Yes/No select whose SSOT value is a full sentence
    carrying a leading Yes/No token ("No. I have no non-compete.", "Yes, I
    would relocate."): map it onto the option that reads EXACTLY "Yes" or
    "No" (case-insensitively strip/first-word), when one exists. Applied only
    AFTER an exact option match has already failed (`_match_option`), never
    as a replacement for it.

    Never guesses a specific "Yes, <detail>" variant from a bare Yes token:
    an option set carrying only "Yes, X" phrasing (no BARE "Yes" option, e.g.
    a sponsorship select enumerating regions) has no single right answer to
    pick, so this returns None and the caller's existing "no option matches"
    skip stays honest rather than fabricating a choice among several
    plausible variants. A leading "No" mapping onto a bare "No" option always
    wins, since a bare negative reads the same regardless of enumeration."""
    verdict = _yesno(raw)
    if verdict is None:
        return None
    target = "yes" if verdict else "no"
    for option in options or []:
        if str(option).strip().lower() == target:
            return option
    return None


# -- yes/no select normalization (criterion: right-to-work / sponsorship) ------

def _select_intent(label: str) -> str | None:
    low = (label or "").lower()
    if _SPONSOR_INTENT_RE.search(low):
        return "sponsorship"
    if _WORK_AUTH_INTENT_RE.search(low):
        return "work_auth"
    return None


def _resolve_yes_no_select(fld, ssot: SSOT, intent: str, raw):
    """Answer a right-to-work / sponsorship select conservatively.

    The region gate takes precedence over a naive exact option match: a posting
    whose label targets a region the SSOT does not cover (e.g. the US) is left
    honestly unfilled with a questionnaire pointer rather than answered from
    EU-context facts. Otherwise an exact option match wins, then a yes/no derived
    from the SSOT work-authorization facts (EU/Italy rights -> Yes to
    authorization / No to sponsorship-required). Never fabricates a Yes for a
    right the SSOT does not state."""
    if _region_ambiguous(fld.label):
        detail = ("region-ambiguous work authorization" if intent == "work_auth"
                  else "region-ambiguous visa sponsorship")
        return None, _questionnaire_skip(
            fld, f"{detail} (posting region outside the SSOT's EU/Italy work "
            "rights)")
    match = _match_option(fld.options, raw)
    if match is not None:
        return match, None
    if intent == "work_auth":
        if not _has_eu_work_rights(_work_auth_text(ssot)):
            return None, _questionnaire_skip(
                fld, "work authorization not established in the SSOT")
        want_yes = True
    else:
        needed = _sponsorship_needed(ssot)
        if needed is None:
            return None, _questionnaire_skip(
                fld, "visa sponsorship requirement not established in the SSOT")
        want_yes = needed                        # sponsorship needed -> Yes
    option = _pick_option(fld.options, want_yes)
    if option is None:
        return None, f"no yes/no option to answer {_short(fld.label)!r}"
    return option, None


def _region_ambiguous(label: str) -> bool:
    """True when the label names a region the SSOT does not cover and does NOT
    also name a covered (EU/Italy) region."""
    return bool(_UNCOVERED_REGION_RE.search(label or "")
                and not _COVERED_REGION_RE.search(label or ""))


def _work_auth_text(ssot: SSOT) -> str:
    raw = ssot.get("work_authorization")
    if raw is MISSING:
        return ""
    if isinstance(raw, dict):
        return " ".join(str(v) for v in raw.values()).lower()
    if isinstance(raw, (list, tuple)):
        return " ".join(str(v) for v in raw).lower()
    return str(raw).lower()


def _has_eu_work_rights(text: str) -> bool:
    if not text:
        return False
    region = re.search(r"\beu\b|european|\beea\b|ital|europe", text)
    rights = re.search(
        r"work right|authori|citizen|permit|entitled|no visa|no sponsor|"
        r"freedom of movement", text)
    return bool(region and rights)


def _sponsorship_needed(ssot: SSOT):
    """True/False/None: does the candidate require visa sponsorship? Prefers the
    dedicated canned answer, then the work-authorization prose."""
    raw = ssot.get("canned_answers.visa_sponsorship_required")
    if raw is not MISSING:
        verdict = _yesno(raw)
        if verdict is not None:
            return verdict
    text = _work_auth_text(ssot)
    if re.search(r"no (visa )?sponsor|sponsorship not (needed|required)|"
                 r"without sponsor|no need for sponsor", text):
        return False
    return None


def _pick_option(options, want_yes: bool):
    """The option whose label reads affirmative (want_yes) or negative. A yes_no
    field with no enumerated options falls back to the literal "Yes"/"No"."""
    for option in options or []:
        if _yesno(option) is want_yes:
            return option
    if not options:
        return "Yes" if want_yes else "No"
    return None


def _yesno(value):
    """True/False/None for a scalar: yes/no leading token, else undetermined."""
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return None
    if _YESNO_NEG_RE.match(text):
        return False
    if _YESNO_POS_RE.match(text):
        return True
    return None


def _questionnaire_skip(fld, detail: str) -> str:
    """A skip reason that both explains the ambiguity and carries a
    questionnaire dotted-path pointer (same shape as fieldmap's missing guess),
    so the required field stays honestly unfilled and feeds a questionnaire."""
    return f"needs questionnaire ({detail}): {_missing_path_guess(fld.label)}"


def _match_option(options, raw):
    """The option label equal (case-insensitively) to a scalar SSOT value."""
    if isinstance(raw, (list, dict)):
        return None
    target = str(raw).strip().lower()
    if not target:
        return None
    for option in options:
        if str(option).strip().lower() == target:
            return option
    return None


def _render_text(raw, path: str):
    if isinstance(raw, bool):
        return ("Yes" if raw else "No"), None
    if isinstance(raw, str):
        if not raw.strip():
            return None, _empty_value_skip(path)
        return raw, None
    if isinstance(raw, (int, float)):
        return str(raw), None
    if isinstance(raw, list) and all(
            isinstance(item, (str, int, float)) for item in raw):
        rendered = ", ".join(str(item) for item in raw)
        if not rendered.strip():
            return None, _empty_value_skip(path)
        return rendered, None
    return None, f"value for {path} is not renderable as text"


def _empty_value_skip(path: str) -> str:
    """The skip reason for a required/answerable field whose SSOT path resolves
    to an empty/whitespace value: there is nothing to fill, so it is SKIPPED
    (never a confirmed fill). A required field with this reason lands in
    `required_unfilled` -> NOT COMPLETE, never a silent false-COMPLETE."""
    return f"empty SSOT value at {path} (nothing to fill)"


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


# -- fill completeness accounting ----------------------------------------------

def _completeness(fieldmap: FieldMap | None, filled_keys: set[str],
                  all_skips: list[tuple[str, str]], filled: int,
                  vendor_resolver=None):
    """Compute (fillable_total, required_unfilled, justified_skips) (criterion 1).

    A required field left unfilled for an UNjustified reason enters
    `required_unfilled` (Z); a non-hidden field left unfilled is counted in
    `justified_skips` only for a justified reason -- a GENUINE demographic-
    section skip (`_is_justified_eeo_skip`: section in COMPLIANCE_EEOC /
    DEMOGRAPHIC / VOLUNTARY, or decline_allowed=True -- regardless of
    requiredness) or an OPTIONAL file-upload/asset-missing skip. A REQUIRED
    field is never justified on EEO grounds merely because its label/reason
    contains an EEO keyword when it is NOT a genuine demographic-section
    field. Hidden portal-telemetry fields are excluded entirely. Without a
    field map the report degrades to the fields fill_form saw and cannot
    assert requiredness, so `required_unfilled` is empty.

    Hidden portal-telemetry detection is delegated to the injected
    `vendor_resolver` (spec 3.4); it defaults to `_NOOP_RESOLVER` (no hidden
    widgets). The Greenhouse behaviour is injected by `engine.fill._completeness`.
    """
    resolver = vendor_resolver if vendor_resolver is not None else _NOOP_RESOLVER
    skip_reason = dict(all_skips)
    if fieldmap is None:
        # No field map means no requiredness to assert (required_unfilled stays
        # empty either way), so an upload skip is counted justified here same
        # as before the fix -- there is no `f.required` to gate it on.
        fillable_total = filled + len(skip_reason)
        justified = sum(1 for reason in skip_reason.values()
                        if _is_eeo_reason(reason) or _is_upload_skip(reason)
                        or _is_satisfied_by_sibling_upload(reason))
        return fillable_total, [], justified

    non_hidden = [f for f in fieldmap.fields if not _is_hidden_field(f, resolver)]
    required_unfilled: list[dict] = []
    justified = 0
    for f in non_hidden:
        if f.key in filled_keys:
            continue
        reason = skip_reason.get(f.key, "not filled")
        if _is_justified_eeo_skip(f, reason):
            justified += 1
        elif _is_satisfied_by_sibling_upload(reason):
            justified += 1
        elif _is_upload_skip(reason) and not f.required:
            justified += 1
        elif f.required:
            required_unfilled.append(
                {"key": f.key, "label": f.label, "reason": reason})
    return len(non_hidden), required_unfilled, justified


def _is_hidden_field(fld, vendor_resolver=_NOOP_RESOLVER) -> bool:
    """Pure portal telemetry (longitude/latitude) is mechanically populated and
    never seen by the applicant, so it is not a fillable denominator field. The
    vendor-specific membership test is delegated to the injected
    `vendor_resolver.hidden_widget` (spec 3.4)."""
    return vendor_resolver.hidden_widget(fld)


def _is_eeo_reason(reason: str) -> bool:
    """True iff the skip reason names an EEO/demographic classification.

    A reason-STRING check ONLY: on its own it does NOT justify a skip. A real
    required question can carry this reason via a mere label-keyword match (the
    `_manual_only_reason` keyword list flags e.g. "disability" on a
    STANDARD-section field), so justification additionally requires the field to
    be a genuine voluntary demographic field -- see `_is_justified_eeo_skip`.
    Used directly only in the no-field-map branch of `_completeness`, where
    requiredness cannot be asserted anyway."""
    low = (reason or "").lower()
    return "demographic" in low or "eeo" in low


def _is_justified_eeo_skip(f, reason: str) -> bool:
    """An EEO/demographic skip is justified for a GENUINE demographic field:
    a COMPLIANCE_EEOC / DEMOGRAPHIC / VOLUNTARY section (`_DECLINE_SECTIONS`),
    or `decline_allowed=True`. This holds REGARDLESS of requiredness: policy
    never auto-answers a real demographic question and decline is always
    allowed there, so even a genuinely demographic field that (unusually)
    carries `required=True` stays justified, never a false gap (Greenhouse's
    own capture already forces `required=False` on these -- `_fields_from_
    question`/`_fields_from_demographic` -- but the gate itself must not
    depend on that normalization holding for every vendor/path).

    A REQUIRED field is NEVER justified on EEO grounds merely because its
    reason string (or its LABEL) happens to contain an EEO keyword: a
    genuinely non-demographic question (STANDARD/CUSTOM/LOCATION section,
    e.g. "disability accommodations needed for the interview?") stays a
    required gap even when `_manual_only_reason`'s keyword-based safety net
    (never auto-fill a suspected-EEO field) fires on its label -- that keyword
    match only prevents auto-fill; it never by itself proves the field is a
    real demographic question. The SECTION (a structural signal set from the
    vendor schema's own section/source tag, never from a label keyword) is the
    gate, not the reason string and not requiredness."""
    return (_is_eeo_reason(reason)
            and (f.decline_allowed
                 or getattr(f, "section", "") in _DECLINE_SECTIONS))


def _is_satisfied_by_sibling_upload(reason: str) -> bool:
    """True iff the skip reason names a satisfied-by-sibling-file-upload
    justification (Greenhouse's `resume_text`/`cover_letter_text` paste
    textarea: the schema exposes it even when the LIVE form is configured
    for file-upload instead of paste-text, so the textarea is simply ABSENT
    from the DOM and never attempted -- the sibling `resume`/`cover_letter`
    file field already carries the same document). Unlike `_is_upload_skip`,
    this is justified REGARDLESS of requiredness: the requirement genuinely
    IS satisfied by the equivalent uploaded artifact, not merely excused
    because the field happens to be optional."""
    return (reason or "").lower().startswith("satisfied by sibling file upload")


def _is_upload_skip(reason: str) -> bool:
    """A file-upload skip: either the legacy no-assets skip ("file-upload") or
    a resolved-but-missing asset ("asset missing: <name>"). Unlike an
    EEO/demographic skip, this is justified ONLY when the field itself is
    OPTIONAL (see the `and not f.required` guard at the call site). A REQUIRED
    upload field left unfilled (no CV/photo attached) is a genuine gap, never a
    free pass -- it must land in `required_unfilled` so `complete` cannot read
    True while a mandatory document was never attached."""
    low = (reason or "").lower()
    return "file-upload" in low or "asset missing" in low
