"""Form-fill DRY RUN for the ToS-safe automatable vendors (W4 4b).

Owner /loop directive (2026-07-03), verbatim intent: this is a DRY RUN in which
the live ATS application pages "receive information on the owner's behalf, STOP
SHORT OF APPLYING". Three real pages (Greenhouse, Lever, Ashby) must actually
have their form fields populated so the owner SEES the filled forms, but no
submission ever happens in this wave.

The module is built so that submitting is not merely "not called" but STRUCTURALLY
absent: there is no code path that clicks a submit control, and several
independent safety invariants (below) fail LOUDLY (`FillSafetyError`) rather than
risk an accidental application.

SAFETY INVARIANTS (load-bearing; enforced in code AND tests):
- No submit code path exists. Every click goes through the single `_safe_click`
  gateway, which refuses any element whose accessible name matches
  `_CLICK_DENYLIST` (submit / apply / send / finish / continue). The happy-path
  fill flow deliberately uses native `fill` / `check` / `select_option` so it
  needs NO clicks at all; `_safe_click` is the sole sanctioned click primitive
  for any future need (e.g. opening a combobox).
- The page URL must be unchanged after the fill (fragment changes allowed). A
  navigation is treated as a possible submission/redirect and raises
  `FillSafetyError`.
- File uploads are WHITELISTED, never arbitrary (owner override of the W5
  deferral, 2026-07-03): the dry run now attaches the CV / profile photo on file
  fields, but ONLY the paths carried by `FillAssets` (the documents/ CVs and the
  profile-pics photo) may be uploaded. `_safe_upload` refuses any other path
  (`FillSafetyError`) and never clicks to open a chooser unless the trigger name
  clears both the submit denylist AND the attach/upload/browse allowlist. The
  owner accepts transmit-on-select for field-level uploads; submission stays
  forbidden.
- EEO / demographic / compliance fields are never touched: they classify as
  manual-only in `fieldmap.coverage` and so never enter the fill set.

W5 DEFERRAL NOTES: the gated real submitter and any click that advances or
submits a form stay deferred to W5's explicitly owner-gated submitter (field
uploads are enabled in this wave per the owner override above). This module is
operator-CLI only and is NEVER wired to the daily timer.

`resolve_values` is deterministic (no LLM): it reuses the `fieldmap` coverage
classifier's resolved dotted paths, resolves each answerable path against the
read-only SSOT, and renders a concrete fill value per field type. `fill_form`
navigates, fills via role/label locators, blurs to harvest validation errors,
reads every control back to diff against intent, screenshots the filled page,
and returns a `FillReport`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from engine.fieldmap import (
    MANUAL_ONLY,
    MISSING_STATUS,
    _DECLINE_SECTIONS,
    _FIRST_NAME_KEYWORDS,
    _LAST_NAME_KEYWORDS,
    _PORTAL_WIDGET_KEYS,
    FieldMap,
    Locator,
    _classify_field,
    _missing_path_guess,
    capture_greenhouse,
)
from engine.providers import registry
from engine.ssot import MISSING, SSOT

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

# -- file-input location (criterion: CV upload on Greenhouse + Lever) ----------
# The fieldmap locator for an upload field is a best-effort role=button hint from
# the questions API, which does NOT reach the real <input type=file>. The file
# input is located directly on the page instead: by a key stem in its id/name,
# else by its `accept` MIME family (document for a CV, image for a photo).
_KEY_STOPWORDS = frozenset({
    "job", "application", "answers", "attributes", "field", "fields",
    "input", "the", "your", "form", "value", "name", "file", "upload",
    "attach", "document", "documents",
})
_DOC_ACCEPT_TOKENS = ("pdf", "doc", "rtf", "txt", "msword",
                      "wordprocessing", "text/", "officedocument")
_IMAGE_ACCEPT_TOKENS = ("image", "png", "jpg", "jpeg", "gif", "webp",
                        "heic", "svg", "bmp", "tiff")

# Click-name denylist: the SINGLE guard that makes a submit structurally
# impossible. Any element whose accessible name matches is refused by
# `_safe_click`, the module's only sanctioned click primitive.
_CLICK_DENYLIST = re.compile(r"submit|apply|send|finish|continue", re.I)

# Upload-trigger allowlist: the file-chooser button path may click a control
# ONLY when its accessible name matches one of these AND clears the submit
# denylist. Belt and braces on top of `_safe_click`.
_UPLOAD_BUTTON_RE = re.compile(r"attach|upload|browse", re.I)

# A candidate-photo control: label reads like a portrait ask (English + Italian)
# or the field is an image-accepting file input (criterion 3).
_PHOTO_LABEL_RE = re.compile(
    r"photo|picture|headshot|profile image|foto|immagine", re.I)

# Posting-language tokens that select the photo CV (cv-atsi) as an informality
# proxy when the form carries no separate candidate-photo field (criterion 2).
_ITALIAN_LANGS = frozenset({"it", "it-it", "italian", "italiano"})

# Preference order when several Me.<ext> portraits exist under a Profile Pics dir.
_PHOTO_EXT_ORDER = (".png", ".jpeg", ".jpg")


class FillSafetyError(RuntimeError):
    """A safety invariant of the dry run was about to be violated.

    Raised (never swallowed) when a click would hit a submit-like control, when
    the page navigated during the fill (possible submission/redirect), or when
    any other STOP-SHORT-OF-APPLYING guard trips. Distinct from a per-field fill
    error (which is fail-soft): a FillSafetyError aborts the whole fill.
    """


# -- upload assets -------------------------------------------------------------

@dataclass
class FillAssets:
    """The whitelisted upload assets: the two CVs and the profile photo.

    Every path is optional and runtime-verified: `verified()` drops any path
    that does not exist on disk to None, so an absent asset becomes a skip
    ("asset missing: <name>") rather than a crash (fail-soft, per the owner
    override). The upload whitelist is EXACTLY these resolved paths;
    `_safe_upload` refuses to upload anything else.
    """
    cv_ats: Path | None = None
    cv_atsi: Path | None = None
    photo: Path | None = None

    def verified(self) -> "FillAssets":
        """A copy whose non-existent asset paths are collapsed to None."""
        return FillAssets(cv_ats=_existing(self.cv_ats),
                          cv_atsi=_existing(self.cv_atsi),
                          photo=_existing(self.photo))

    def is_whitelisted(self, path) -> bool:
        """True iff `path` resolves to one of the (existing) asset paths."""
        target = _resolved(path)
        if target is None:
            return False
        return any(_resolved(asset) == target
                   for asset in (self.cv_ats, self.cv_atsi, self.photo)
                   if asset is not None)


def _existing(path) -> Path | None:
    if path is None:
        return None
    candidate = Path(path).expanduser()
    return candidate if candidate.exists() else None


def _resolved(path) -> Path | None:
    if path is None:
        return None
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


# -- resolved fill values ------------------------------------------------------

@dataclass
class FieldValue:
    """One concrete field to fill: the rendered value plus the locator hints and
    type needed to reach and drive the control (fill_form gets no fieldmap).

    For an upload field the `value` is the chosen asset `Path`; `asset` records
    which asset ("cv-ats" | "cv-atsi" | "photo") and `upload_reason` records why
    (owner calibration signal for the CV selection rule)."""
    key: str
    label: str
    type: str
    locator: Locator
    value: str | bool | list | Path
    asset: str | None = None
    upload_reason: str | None = None


@dataclass
class ResolvedValues:
    """The deterministic output of `resolve_values`: the fillable fields (with
    the metadata fill_form needs) plus the fields skipped with their reasons.

    `.values` exposes the documented `dict[str, str|bool|list]` key->value view;
    fill_form consumes the richer `.fields`/`.skipped` directly.
    """
    fields: list[FieldValue] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def values(self) -> dict[str, str | bool | list]:
        return {fv.key: fv.value for fv in self.fields}


@dataclass
class FillReport:
    """The evidence of one fill, with a completeness denominator (criterion 1).

    `fillable_total` (Y) is every non-hidden field on the field map; `filled`
    (X) is how many were actually populated AND readback-CONFIRMED (uploads
    included) -- a value the page silently rejected, or an upload a custom
    widget swallowed without ever wiring the native input, never increments X.
    `required_unfilled` (Z) lists every required field left unfilled for an
    UNJUSTIFIED reason -- this INCLUDES a required file-upload field whose
    asset is missing or was never attached, AND a required field whose
    readback did not confirm the value (a value the page silently rejected),
    so a mandatory answer or CV/photo that never made it onto the page can
    never read as done. A REQUIRED field whose SSOT answer resolves to
    empty/whitespace is likewise a gap (nothing landed), never a confirmed
    fill. `justified_skips` counts non-hidden fields left unfilled for a
    justified reason: an EEO/demographic skip ONLY when the field is a GENUINE
    demographic field (a demographic / EEOC / voluntary section, or
    decline_allowed=True -- regardless of requiredness, decline is always
    allowed there) -- a REQUIRED field is never EEO-justified merely because
    its label carries an EEO keyword when it is not really a demographic-
    section field -- or a file-upload/asset-missing skip on an OPTIONAL field
    only. `complete` is True iff there are no required gaps (Z == 0); an
    optional field left unfilled for any other reason does not, by itself,
    force NOT COMPLETE -- the X/Y counts already surface that partial coverage,
    and a required gap is the hard fail.
    """
    vendor: str
    company: str
    posting_id: str
    fillable_total: int
    filled: int
    required_unfilled: list[dict]
    justified_skips: int
    uploads: list[dict]
    skipped: list[tuple[str, str]]
    readback_mismatches: list[dict]
    validation_errors: list[dict]
    url_unchanged: bool
    screenshot: str
    ts: str

    @property
    def complete(self) -> bool:
        return not self.required_unfilled

    def caption(self) -> str:
        """The owner-mandated notification caption (criterion 1), exact shape:

            <Vendor> (<company>): X/Y fields filled, Z required unfilled - COMPLETE

        with "NOT COMPLETE" whenever Z > 0 (a required field -- including a
        required file-upload with a missing asset, or ANY required field whose
        readback did not confirm the value took -- was left unfilled for a
        non-justified reason). An optional field left unfilled does not, on
        its own, flip this to NOT COMPLETE. The evidence publisher sends THIS
        as the ntfy message, so the verdict rides the notification the owner
        reads.
        """
        status = "COMPLETE" if self.complete else "NOT COMPLETE"
        return (f"{self.vendor.capitalize()} ({self.company}): "
                f"{self.filled}/{self.fillable_total} fields filled, "
                f"{len(self.required_unfilled)} required unfilled - {status}")

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "company": self.company,
            "posting_id": self.posting_id,
            "fillable_total": self.fillable_total,
            "filled": self.filled,
            "required_unfilled": list(self.required_unfilled),
            "justified_skips": self.justified_skips,
            "uploads": list(self.uploads),
            "complete": self.complete,
            "caption": self.caption(),
            "skipped": [[key, reason] for key, reason in self.skipped],
            "readback_mismatches": self.readback_mismatches,
            "validation_errors": self.validation_errors,
            "url_unchanged": self.url_unchanged,
            "screenshot": self.screenshot,
            "ts": self.ts,
        }


# -- deterministic value resolution --------------------------------------------

def resolve_values(fieldmap: FieldMap, ssot: SSOT, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en") -> ResolvedValues:
    """Classify + render every field of `fieldmap` into concrete fill values.

    File-upload fields resolve to a whitelisted asset (owner override): a
    candidate-photo field gets the profile photo, every other file field gets a
    CV picked by the deterministic rule (cv-ats by default; cv-atsi ONLY when the
    form has no photo field AND `posting_lang` is Italian). With no `assets`
    (the pre-override default) file fields keep the old "file-upload" skip, so
    the existing contract holds.

    A checkbox (boolean) is resolved by its label intent (`_resolve_boolean`): a
    consent/confirmation box ticks True when the SSOT ratifies consent, a
    talent-pool box ticks True, a marketing box is left unticked. Every other
    field reuses `fieldmap._classify_field` (the SSOT coverage classifier):
    manual-only (EEO-demographic / portal widget) and missing (unanswerable)
    fields are SKIPPED with their classifier reason. An answerable field is
    rendered by type: free text from the resolved SSOT string, and an option
    label for a select (an exact case-insensitive option match, else a yes/no
    normalization for right-to-work / sponsorship questions, else skipped).
    Deterministic, no LLM; never writes the SSOT.
    """
    profile = profile or {}
    assets = assets.verified() if assets is not None else None
    resolved = ResolvedValues()
    has_photo_field = _form_has_photo_field(fieldmap)
    for fld in fieldmap.fields:
        if _is_upload_field(fld):
            _resolve_upload(fld, resolved, assets, posting_lang, has_photo_field)
            continue
        if (fld.type or "").lower() == "boolean":
            _resolve_boolean(fld, resolved, ssot, profile)
            continue
        classified = _classify_field(fld, ssot, profile)
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


def _is_upload_field(fld) -> bool:
    """A field the dry run now uploads to: a real file input, or a control whose
    label carries an explicit upload/attach verb (a strong file signal). A bare
    resume/cv label on a NON-file control stays with the coverage classifier
    (manual-only), so a "CV link" text box is never fed a document."""
    if "file" in (fld.type or "").lower():
        return True
    label = (fld.label or "").lower()
    return "upload" in label or "attach" in label


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


def _resolve_upload(fld, resolved: ResolvedValues, assets: FillAssets | None,
                    posting_lang: str, has_photo_field: bool) -> None:
    if assets is None:
        # Pre-override contract: no assets -> file fields are skipped, not filled.
        resolved.skipped.append((fld.key, "file-upload"))
        return
    if _is_photo_field(fld):
        asset_name, path, reason = ("photo", assets.photo,
                                    "candidate photo/portrait field")
    else:
        asset_name, path, reason = _select_cv(assets, posting_lang, has_photo_field)
    if path is None:
        resolved.skipped.append((fld.key, f"asset missing: {asset_name}"))
        return
    resolved.fields.append(FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=path, asset=asset_name, upload_reason=reason))


def _select_cv(assets: FillAssets, posting_lang: str, has_photo_field: bool):
    """The deterministic v1 CV rule (criterion 2): cv-ats by default; cv-atsi
    ONLY when the form has no separate photo field AND the posting is Italian
    (informal-company proxy, flagged in the report for owner calibration)."""
    if not has_photo_field and _is_italian(posting_lang):
        return ("cv-atsi", assets.cv_atsi,
                "italian posting and no photo field (informal-company proxy)")
    return "cv-ats", assets.cv_ats, "default (cv-ats always preferred)"


def _is_italian(posting_lang: str) -> bool:
    return str(posting_lang or "").strip().lower() in _ITALIAN_LANGS


# -- checkbox (boolean) resolution ---------------------------------------------

def _resolve_boolean(fld, resolved: ResolvedValues, ssot: SSOT,
                     profile: dict) -> None:
    """Resolve a checkbox by its label intent (criterion: consent checkboxes).

    An EEO/demographic or file boolean stays manual-only (never auto-answered).
    A consent/confirmation box is ticked True when the SSOT ratifies consent; a
    talent-pool / future-opportunities box is ticked True (YES per the owner
    split); a marketing/newsletter box is left unticked; any other checkbox is
    left for a human (unchanged pre-existing behaviour)."""
    classified = _classify_field(fld, ssot, profile)
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


# -- the fill itself -----------------------------------------------------------

def fill_form(vendor: str, slug: str, job_id: str, values: ResolvedValues,
              browser_factory=None, artifacts_dir=None, *,
              fieldmap: FieldMap | None = None,
              assets: FillAssets | None = None,
              now: Callable[[], str] | None = None) -> FillReport:
    """Fill one live application page with `values`, STOPPING SHORT OF APPLYING.

    Navigates to the vendor apply page, fills each resolved value via a role/label
    locator (reusing the fieldmap locator hint, falling back to label text),
    uploads any whitelisted asset via `_safe_upload` (no submit ever), blurs after
    each fill to harvest validation errors (aria-invalid + `.error` text), reads
    every filled control back to diff against intent, screenshots the filled page,
    and asserts the page URL is unchanged. A field (or upload) counts toward
    `filled` ONLY when its readback confirms the value actually landed -- a
    value the page silently rejected, or an upload a custom widget swallowed
    without wiring the native input, is excluded from `filled` and, if the
    field is required, becomes a `required_unfilled` gap (never a silent
    false-COMPLETE). When `fieldmap` is supplied the report carries the
    completeness denominator (criterion 1); without it the report degrades to
    the fields it saw. A navigation or a submit-like click raises
    FillSafetyError.
    """
    ts = (now or _utc_now_iso)()
    url = _apply_url(vendor, slug, job_id)
    factory = browser_factory or _default_browser_page

    readback_mismatches: list[dict] = []
    validation_errors: list[dict] = []
    extra_skips: list[tuple[str, str]] = []
    uploads: list[dict] = []
    filled_keys: set[str] = set()

    with factory() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        pre_url = _current_url(page)

        for fv in values.fields:
            if _is_upload(fv):
                _fill_upload(page, fv, assets, uploads, extra_skips, filled_keys)
                continue
            if fv.type == "input_file":
                # Defence in depth: a file field with no whitelisted asset is
                # never driven as text (no set_input_files, no fill).
                extra_skips.append(
                    (fv.key, "file-upload without a whitelisted asset"))
                continue
            try:
                locator = _locate(page, fv)
                _apply(locator, fv)
                locator.blur()
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field fill error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
                continue
            _harvest_field_validation(locator, fv, validation_errors)
            actual, ok = _readback(locator, fv.value)
            if ok:
                # Only a readback-CONFIRMED value counts as filled: a value the
                # page silently rejected (or a custom control that swallowed it)
                # must never read as done.
                filled_keys.add(fv.key)
            else:
                readback_mismatches.append(
                    {"key": fv.key, "intended": fv.value, "actual": actual})
                extra_skips.append(
                    (fv.key, "value did not take (readback mismatch)"))

        _harvest_page_errors(page, validation_errors)
        screenshot = _screenshot(page, vendor, job_id, ts, artifacts_dir)
        post_url = _current_url(page)

    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled)

    return FillReport(
        vendor=vendor, company=slug, posting_id=str(job_id),
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=uploads, skipped=all_skips,
        readback_mismatches=readback_mismatches,
        validation_errors=validation_errors,
        url_unchanged=url_unchanged, screenshot=str(screenshot), ts=ts)


def _is_upload(fv: FieldValue) -> bool:
    """An upload field carries its chosen asset as a Path value."""
    return isinstance(fv.value, Path)


def _fill_upload(page, fv: FieldValue, assets: FillAssets | None,
                 uploads: list[dict], extra_skips: list[tuple[str, str]],
                 filled_keys: set[str]) -> None:
    """Upload one whitelisted asset to the real page file input; a FillSafetyError
    still aborts the whole run, a per-field failure is fail-soft. A successful
    upload counts toward filled ONLY once `_upload_attached` confirms via
    readback that a file actually landed on the input -- a silently swallowed
    attach (e.g. a custom widget that never wires the native input) is excluded
    from filled and, if required, becomes a required gap.

    The fieldmap locator (best-effort role=button from the questions API) does
    NOT reach the actual <input type=file> on Greenhouse/Lever, so the input is
    located directly (`_locate_file_input`) and driven via `set_input_files` with
    no click. A required upload with no matching input stays required_unfilled."""
    if assets is None:
        extra_skips.append((fv.key, "upload skipped: no FillAssets provided"))
        return
    control = _locate_file_input(page, fv)
    if control is None:
        extra_skips.append((fv.key, "no file input located"))
        return
    try:
        _safe_upload(control, fv.value, assets, page=page,
                     button_name=fv.locator.name or fv.label)
    except FillSafetyError:
        raise
    except Exception as exc:  # per-field upload error is fail-soft
        extra_skips.append((fv.key, f"upload-error: {exc}"))
        return
    if not _upload_attached(control):
        extra_skips.append((fv.key, "upload did not attach (readback)"))
        return
    filled_keys.add(fv.key)
    uploads.append({"key": fv.key, "asset": fv.asset,
                    "path": str(fv.value), "reason": fv.upload_reason})


def _upload_attached(control) -> bool:
    """True iff the file input's readback shows a file actually attached after
    `set_input_files`: a genuine attach leaves the control's value non-empty
    (a real browser reports a fakepath filename); a control the upload silently
    passed through without wiring the native input reads back empty and must
    not count as filled. A control with no readable value (no `input_value`) is
    assumed attached -- real <input type=file> handles always expose it; this
    is defence in depth only, matching `_safe_get_attr`'s read-or-default
    pattern."""
    getter = getattr(control, "input_value", None)
    if not callable(getter):
        return True
    try:
        value = getter()
    except Exception:
        return True
    return bool(value)


def _locate_file_input(page, fv: FieldValue):
    """Find the real <input type=file> for an upload field.

    Preference (per the live probe of Greenhouse/Lever/Ashby): an input whose
    id or name contains a meaningful token of the field key (e.g. "resume");
    else, by `accept` MIME family: an image input for a photo field, a document
    input (or an input with no `accept`) for a CV field. None if none matches."""
    inputs = _file_inputs(page)
    if not inputs:
        return None
    tokens = _field_key_tokens(fv.key)
    for inp in inputs:
        idname = _input_idname(inp)
        if idname and any(token in idname for token in tokens):
            return inp
    want_image = fv.asset == "photo"
    for inp in inputs:
        accept = _input_accept(inp)
        if want_image:
            if _accept_has_image(accept):
                return inp
        elif not accept or _accept_has_doc(accept):
            return inp
    return None


def _file_inputs(page):
    getter = getattr(page, "query_selector_all", None)
    if getter is None:
        return []
    try:
        return list(getter("input[type=file]") or [])
    except Exception:
        return []


def _field_key_tokens(key: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", (key or "").lower())
            if len(token) >= 3 and token not in _KEY_STOPWORDS]


def _input_idname(inp) -> str:
    idv = _safe_get_attr(inp, "id") or ""
    namev = _safe_get_attr(inp, "name") or ""
    return f"{idv} {namev}".lower()


def _input_accept(inp) -> str:
    return (_safe_get_attr(inp, "accept") or "").lower()


def _accept_has_doc(accept: str) -> bool:
    return any(token in accept for token in _DOC_ACCEPT_TOKENS)


def _accept_has_image(accept: str) -> bool:
    return any(token in accept for token in _IMAGE_ACCEPT_TOKENS)


def _completeness(fieldmap: FieldMap | None, filled_keys: set[str],
                  all_skips: list[tuple[str, str]], filled: int):
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
    """
    skip_reason = dict(all_skips)
    if fieldmap is None:
        # No field map means no requiredness to assert (required_unfilled stays
        # empty either way), so an upload skip is counted justified here same
        # as before the fix -- there is no `f.required` to gate it on.
        fillable_total = filled + len(skip_reason)
        justified = sum(1 for reason in skip_reason.values()
                        if _is_eeo_reason(reason) or _is_upload_skip(reason))
        return fillable_total, [], justified

    non_hidden = [f for f in fieldmap.fields if not _is_hidden_field(f)]
    required_unfilled: list[dict] = []
    justified = 0
    for f in non_hidden:
        if f.key in filled_keys:
            continue
        reason = skip_reason.get(f.key, "not filled")
        if _is_justified_eeo_skip(f, reason):
            justified += 1
        elif _is_upload_skip(reason) and not f.required:
            justified += 1
        elif f.required:
            required_unfilled.append(
                {"key": f.key, "label": f.label, "reason": reason})
    return len(non_hidden), required_unfilled, justified


def _is_hidden_field(fld) -> bool:
    """Pure portal telemetry (longitude/latitude) is mechanically populated and
    never seen by the applicant, so it is not a fillable denominator field."""
    return (fld.key or "").lower() in _PORTAL_WIDGET_KEYS


def _is_eeo_reason(reason: str) -> bool:
    """True iff the skip reason names an EEO/demographic classification.

    A reason-STRING check ONLY: on its own it does NOT justify a skip. A real
    required question can carry this reason via a mere label-keyword match (the
    `fieldmap._manual_only_reason` keyword list flags e.g. "disability" on a
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


def _safe_click(target, name: str) -> None:
    """The SOLE sanctioned click primitive: refuses any submit-like name.

    Every click need (there are none on the happy path; a combobox open would be
    one) must route through here so a submit is structurally impossible.
    """
    if _CLICK_DENYLIST.search(name or ""):
        raise FillSafetyError(
            f"refusing to click element named {name!r}: matches the submit "
            "denylist (submit/apply/send/finish/continue); this dry run STOPS "
            "SHORT OF APPLYING")
    target.click()


def _safe_upload(control, path, assets: FillAssets, *,
                 page=None, button_name: str | None = None) -> None:
    """Attach a WHITELISTED asset to a file control without ever submitting.

    (a) Whitelist: `path` MUST resolve to one of the FillAssets (the documents/
        CVs or the profile-pics photo); anything else raises FillSafetyError.
    (b) Never clicks blindly: uploads via `set_input_files` directly on the input
        (Playwright drives even hidden inputs). Only when the control exposes no
        file input does it fall back to the OS file chooser, and then the button
        click is routed through `_safe_click` (submit denylist) ONLY when the
        trigger name also matches the attach/upload/browse allowlist.
    """
    if not assets.is_whitelisted(path):
        raise FillSafetyError(
            f"refusing to upload {str(path)!r}: not in the FillAssets whitelist "
            "(only the documents/ CVs and the profile-pics photo may be uploaded)")
    setter = getattr(control, "set_input_files", None)
    if callable(setter):
        setter(str(path))            # direct: no click, works on hidden inputs
        return
    if page is None or button_name is None:
        raise FillSafetyError(
            "cannot upload: the control exposes no file input and no file-chooser "
            "trigger is available")
    if not _UPLOAD_BUTTON_RE.search(button_name):
        raise FillSafetyError(
            f"refusing to open a file chooser via {button_name!r}: the trigger "
            "name does not match the attach/upload/browse allowlist")
    with page.expect_file_chooser() as chooser:
        _safe_click(control, button_name)     # also enforces the submit denylist
    chooser.value.set_files(str(path))


def _apply(locator, fv: FieldValue) -> None:
    """Write one value using a native form action (no click, never a submit)."""
    value = fv.value
    if isinstance(value, bool):
        locator.check()          # ticks a checkbox; cannot submit a form
    elif isinstance(value, list):
        locator.select_option(label=value)
    elif fv.type in _SELECT_TYPES:
        locator.select_option(label=value)
    else:
        locator.fill(value)


def _locate(page, fv: FieldValue):
    """Reuse the fieldmap locator hint (role+name); fall back to the label."""
    role = fv.locator.role
    name = fv.locator.name
    if role and name:
        return page.get_by_role(role, name=name)
    return page.get_by_label(fv.label)


def _readback(locator, value):
    """Read the control's current value back and decide if it matches intent.

    An empty/whitespace INTENDED text value is NEVER a confirmed fill: `fill`/
    `type_human` place nothing in the control (type_human early-returns on empty),
    so a blank control reading back "equal" to a blank intent would be a false
    match. Such a field is reported unconfirmed (ok=False) so a required field
    whose SSOT answer is empty becomes a required gap, never a silent
    false-COMPLETE. (The resolve layer already skips blank values before they
    reach here; this is the defence-in-depth guard at the readback boundary that
    covers every provider sharing `_readback`.)
    """
    if isinstance(value, bool):
        actual = locator.is_checked()
        return actual, bool(actual) == value
    if isinstance(value, list):
        actual = locator.input_value()
        haystack = str(actual).lower()
        return actual, all(str(item).strip().lower() in haystack
                           for item in value)
    actual = locator.input_value()
    if not _norm(value):
        return actual, False
    return actual, _norm(actual) == _norm(value)


def _harvest_field_validation(locator, fv: FieldValue, out: list[dict]) -> None:
    aria = _safe_get_attr(locator, "aria-invalid")
    if aria is not None and str(aria).strip().lower() == "true":
        out.append({"key": fv.key, "message": "aria-invalid"})


def _harvest_page_errors(page, out: list[dict]) -> None:
    """Collect any `.error` text rendered on the page after blur."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return
    try:
        texts = locator_fn(".error").all_inner_texts()
    except Exception:
        return
    for text in texts or []:
        if text and str(text).strip():
            out.append({"message": str(text).strip()})


def _screenshot(page, vendor: str, job_id: str, ts: str, artifacts_dir) -> Path:
    base = Path(artifacts_dir) if artifacts_dir else Path.cwd() / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"fill-{vendor}-{job_id}-{_safe_stamp(ts)}.png"
    page.screenshot(path=str(path))
    return path


# -- playwright lifecycle (delegated to browse.py; lazy import) ----------------

def _default_browser_page():
    """The real headless-chromium page factory (imported lazily via browse.py).

    Kept as a thin indirection so this module imports cleanly without playwright
    and only reaches for it when a real fill is actually invoked with no fake
    factory. Tests always pass a fake factory and never touch this.
    """
    from engine.browse import _default_browser_page as real_factory
    return real_factory()


_GOTO_TIMEOUT_MS = 20_000


# -- URL helpers ---------------------------------------------------------------

def greenhouse_apply_url(slug: str, job_id: str) -> str:
    """The public Greenhouse apply page (the `job-boards.greenhouse.io/{slug}/
    jobs/{job_id}` host is the newer variant of the same page)."""
    return f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"


def _apply_url(vendor: str, slug: str, job_id: str) -> str:
    spec = registry.PROVIDERS.get(vendor)
    if spec is None or not spec.supported or spec.apply_url_fn is None:
        raise ValueError(
            f"unknown vendor {vendor!r} (expected greenhouse/lever/ashby/workable)")
    return spec.apply_url_fn(slug, job_id)


def _current_url(page) -> str:
    return getattr(page, "url", "") or ""


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


# -- small helpers -------------------------------------------------------------

def _norm(value) -> str:
    return str(value).strip().lower()


def _safe_get_attr(locator, name: str):
    getter = getattr(locator, "get_attribute", None)
    if getter is None:
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _safe_stamp(ts: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", ts) or "run"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- toto assets + evidence publishing -----------------------------------------

def default_assets(*, documents_dir=None, archive_root=None,
                   cv_ats=None, cv_atsi=None, photo=None) -> FillAssets:
    """Resolve the toto upload assets, existence-checked (fail-soft, criterion 5).

    Defaults follow the toto layout: documents/cv-ats.pdf, documents/cv-atsi.pdf,
    and the first Me.<png|jpeg|jpg> under a 'Profile Pic*' directory of the
    career archive. Any explicit path overrides its default; an absent asset
    collapses to None (a later "asset missing" skip) rather than crashing.
    """
    docs = (Path(documents_dir).expanduser() if documents_dir
            else Path.home() / "automations" / "documents")
    ats = Path(cv_ats).expanduser() if cv_ats else docs / "cv-ats.pdf"
    atsi = Path(cv_atsi).expanduser() if cv_atsi else docs / "cv-atsi.pdf"
    return FillAssets(cv_ats=ats, cv_atsi=atsi,
                      photo=_resolve_photo(photo, archive_root)).verified()


def _resolve_photo(override=None, archive_root=None) -> Path | None:
    """Find the profile portrait: an explicit override, else the first existing
    Me.<ext> (png > jpeg > jpg) under a case-insensitive 'Profile Pic*' dir of
    the career archive (the exact dir name may drift, so the search globs it)."""
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.exists() else None
    root = (Path(archive_root).expanduser() if archive_root
            else Path.home() / "automations" / "career-archive")
    if not root.exists():
        return None
    matches = [p for p in root.rglob("*")
               if p.is_file()
               and p.stem.lower() == "me"
               and p.suffix.lower() in _PHOTO_EXT_ORDER
               and p.parent.name.lower().startswith("profile pic")]
    if not matches:
        return None
    matches.sort(key=lambda p: _PHOTO_EXT_ORDER.index(p.suffix.lower()))
    return matches[0]


def publish_evidence(report: FillReport, topic: str, transport) -> None:
    """Publish the fill screenshot to `topic`, captioned by FillReport.caption()
    (criterion 4). The message is EXACTLY the caption (no hand-written captions),
    so the completeness verdict rides the notification the owner reads. Operator
    only; the transport is injected (FakeTransport in tests, NtfyTransport live).
    """
    transport.publish_file(topic, report.screenshot, report.caption(),
                           Path(report.screenshot).name)


# -- operator CLI --------------------------------------------------------------

def _load_fieldmap(store, vendor: str, slug: str, job_id: str, *,
                   updated_at: str | None, capture_first: bool) -> FieldMap:
    """Load a cached field map, or (only with --capture-first) capture fresh.

    The store key is (vendor, posting_id, updated_at); `updated_at` defaults to
    "" (the same key put_fieldmap writes when a posting carries no board
    timestamp). On a miss the operator must opt into a live capture.
    """
    cached = store.get_fieldmap(vendor, job_id, updated_at or "")
    if cached is not None:
        return FieldMap.from_dict(cached["body"])
    if not capture_first:
        raise SystemExit(
            f"no cached field map for {vendor} {slug}/{job_id}; pass "
            "--capture-first to capture it live (browser/HTTP egress)")
    fieldmap = _capture(vendor, slug, job_id)
    store.put_fieldmap(vendor, job_id, updated_at or "",
                       fieldmap.to_dict(), fieldmap.captured_at)
    return fieldmap


def _capture(vendor: str, slug: str, job_id: str) -> FieldMap:
    if vendor == "greenhouse":
        return capture_greenhouse(slug, job_id)
    # Lever/Ashby need the browser; imported here so the module and the
    # --fieldmap-from-store path stay usable without playwright installed.
    from engine.browse import capture_ashby, capture_lever
    if vendor == "lever":
        return capture_lever(slug, job_id)
    if vendor == "ashby":
        return capture_ashby(slug, job_id)
    raise SystemExit(f"unknown vendor {vendor!r} (expected greenhouse/lever/ashby)")


def _parse_args(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog="engine.fill",
        description="Form-fill DRY RUN (operator only; STOPS SHORT OF APPLYING)")
    parser.add_argument("--vendor", required=True,
                        choices=["greenhouse", "lever", "ashby"])
    parser.add_argument("--slug", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--ssot", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--artifacts")
    parser.add_argument("--updated-at", default=None,
                        help="the board updated_at that keys the cached field "
                             "map (defaults to the empty key)")
    parser.add_argument("--posting-lang", default="en",
                        help="posting language hint (it selects the photo CV as "
                             "an informality proxy when no photo field exists)")
    parser.add_argument("--cv-ats",
                        help="override the default documents/cv-ats.pdf")
    parser.add_argument("--cv-atsi",
                        help="override the default documents/cv-atsi.pdf")
    parser.add_argument("--photo",
                        help="override the resolved career-archive portrait")
    parser.add_argument("--publish-topic",
                        help="ntfy topic to publish the screenshot + caption to")
    parser.add_argument("--ntfy-credentials",
                        help="path to the ntfy credentials file (defaults to "
                             "~/automations/ntfy/credentials)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--fieldmap-from-store", action="store_true",
                        help="use only a cached field map (error on a miss)")
    source.add_argument("--capture-first", action="store_true",
                        help="capture the field map live if the cache misses")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    from engine.profile_map import profile_from_real_ssot
    from engine.store import Store

    ssot = SSOT.load(args.ssot)
    profile = profile_from_real_ssot(ssot)
    assets = default_assets(cv_ats=args.cv_ats, cv_atsi=args.cv_atsi,
                            photo=args.photo)
    store = Store(args.store)
    try:
        fieldmap = _load_fieldmap(
            store, args.vendor, args.slug, args.job_id,
            updated_at=args.updated_at, capture_first=args.capture_first)
        values = resolve_values(fieldmap, ssot, profile, assets=assets,
                                posting_lang=args.posting_lang)
        report = fill_form(args.vendor, args.slug, args.job_id, values,
                           fieldmap=fieldmap, assets=assets,
                           artifacts_dir=args.artifacts)
    finally:
        store.close()
    if args.publish_topic:
        _publish_cli(report, args.publish_topic, args.ntfy_credentials)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


def _publish_cli(report: FillReport, topic: str, credentials_path) -> None:
    from engine.notify import NtfyTransport, load_credentials

    path = credentials_path or str(
        Path.home() / "automations" / "ntfy" / "credentials")
    publish_evidence(report, topic, NtfyTransport(load_credentials(path)))


if __name__ == "__main__":
    import sys

    sys.exit(main())
