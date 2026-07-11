"""Greenhouse portal-widget coverage resolver (W5.1 Stage 2a; moved from
engine.fieldmap).

The `vendor_resolver` (spec 3.4) the kernel coverage classifier consults for
Greenhouse's location-autocomplete `location` field, the paste-in
`resume_text`/`cover_letter_text` textareas, and the `longitude`/`latitude`
portal-telemetry fields. It is injected EXPLICITLY by the live callers: the
pipeline (`engine.run`) builds it per vendor from the registry and passes it into
`coverage`; `engine.providers.greenhouse.fill` passes it into the kernel
resolve/completeness calls; the test harness passes an explicit `vendor_resolver`.
The kernel default is `_NOOP_RESOLVER` (a vendor with no portal-widget quirks),
never this resolver.
"""

from __future__ import annotations

from engine.kernel.contracts import Field
from engine.kernel.ssot import MISSING, SSOT

# Greenhouse's location-autocomplete widget (round-1 live-capture finding):
# the composite question shares one label across three sub-fields keyed
# `location`/`longitude`/`latitude`. All three are mechanically populated by
# the portal's JS widget, never typed by the applicant, so they are matched
# by KEY (the shared label carries no distinguishing text) ahead of the
# generic label matchers. `location` still resolves to real applicant data
# (the address); `longitude`/`latitude` are pure portal telemetry.
_LOCATION_WIDGET_KEY = "location"
_PORTAL_WIDGET_KEYS = {"longitude", "latitude"}

# Greenhouse's paste-in resume/cover-letter textareas (`resume_text`,
# `cover_letter_text`) share their label ("Resume"/"Resume/CV") with the
# sibling FILE upload field, so label keyword matching alone cannot
# distinguish them: they are matched by KEY, ahead of the generic label
# matchers, same as `_LOCATION_WIDGET_KEY` above. Each maps to the ordered
# SSOT dotted paths tried in turn; the first that resolves in the SSOT wins.
_KEY_TEXT_PATHS = {
    "resume_text": ("canned_answers.resume_text", "documents.cv_text"),
    "cover_letter_text": ("canned_answers.cover_letter_text",
                          "canned_answers.cover_letter"),
}


def _location_widget_path(fld: Field, ssot: SSOT) -> str | None:
    """The Greenhouse location-autocomplete widget's `location` sub-field is
    mechanically populated (never typed by the applicant): resolve it via the
    identity address directly, ahead of the generic label matchers (its label
    is shared with the manual-only `longitude`/`latitude` siblings, so label
    keyword matching alone cannot distinguish it)."""
    if fld.key.lower() != _LOCATION_WIDGET_KEY:
        return None
    # The real seeded SSOT (v1.4) keys this as identity.current_location;
    # identity.address is kept as a legacy fallback for schema drift.
    for candidate in ("identity.current_location", "identity.address"):
        if ssot.get(candidate) is not MISSING:
            return candidate
    return None


def _key_text_widget_path(fld: Field, ssot: SSOT) -> str | None:
    """The Greenhouse `resume_text`/`cover_letter_text` paste textareas' label
    ("Resume"/"Resume/CV") is shared with the sibling FILE upload field, so
    label keyword matching alone cannot distinguish them: resolve by KEY
    instead, same pattern as `_location_widget_path`. Returns None (MISSING)
    when the SSOT carries neither candidate path -- never fabricated; the
    owner seeds `canned_answers` then re-runs."""
    candidates = _KEY_TEXT_PATHS.get(fld.key.lower())
    if candidates is None:
        return None
    for candidate in candidates:
        if ssot.get(candidate) is not MISSING:
            return candidate
    return None


class _GreenhouseWidgetResolver:
    """Greenhouse portal-widget resolver.

    The `vendor_resolver` (spec 3.4) the kernel classifier consults for
    Greenhouse's location-autocomplete `location` field, the paste-in
    `resume_text`/`cover_letter_text` textareas, and the `longitude`/`latitude`
    portal-telemetry fields. Each method MIRRORS the exact membership test the
    pre-extraction `engine.fieldmap`/`engine.fill` code used: `manual_reason`
    keeps `fld.key.lower()` (the old `_manual_only_reason` portal branch) and
    `hidden_widget` keeps `(fld.key or "").lower()` (the old `_is_hidden_field`)."""
    def location_path(self, fld, ssot): return _location_widget_path(fld, ssot)
    def key_text_path(self, fld, ssot): return _key_text_widget_path(fld, ssot)
    def manual_reason(self, fld):
        return "portal-widget" if fld.key.lower() in _PORTAL_WIDGET_KEYS else ""
    def hidden_widget(self, fld):
        return (fld.key or "").lower() in _PORTAL_WIDGET_KEYS


GREENHOUSE_WIDGET_RESOLVER = _GreenhouseWidgetResolver()
