"""Kernel data contracts (W5.1 stage 0).

The dataclasses/enums shared by every vendor plugin and by the kernel's own
fill/fieldmap/discover modules. This is the base of the kernel: at LOAD time it
imports only the standard library, so any provider or kernel module can depend
on it without pulling in classification/automation logic. One documented,
transitional CALL-TIME seam remains: `FieldMap.coverage` delegates to the
`engine.fieldmap.coverage` shim (which default-injects the Greenhouse widget
resolver for today's method callers; see the method comment). The classifier
itself already lives in `engine.kernel.resolve` (moved in W5.1 Stage 1); the
seam dies in Stage 3 when callers inject a registry-built vendor_resolver,
after which this module references nothing outside the kernel.

Moved verbatim from `engine.fieldmap` / `engine.fill` / `engine.discover`
(W5.1 stage 0); each origin module now re-exports these names via a shim
import so existing importers (tests, providers, run.py) keep resolving to
the SAME objects. See those modules' own docstrings for the domain context
these types serve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# == moved from engine.fieldmap =============================================

SCHEMA_VERSION = "2"


class FieldType:
    """The unified FieldSchema type vocabulary (W5 angle-5 spec, section 3).

    `Field.norm_type` (schema_version 2+) carries one of these, independent of
    the vendor-native `type` string kept in `Field.type` for backward
    compatibility with fill.py/coverage's existing string matching.
    """

    TEXT = "TEXT"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    URL = "URL"
    NUMBER = "NUMBER"
    DATE = "DATE"
    LONGTEXT = "LONGTEXT"
    SINGLE_SELECT = "SINGLE_SELECT"
    MULTI_SELECT = "MULTI_SELECT"
    BOOLEAN = "BOOLEAN"
    FILE = "FILE"


class Section:
    """The unified FieldSchema section vocabulary (W5 angle-5 spec, section 3).

    `Field.section` (schema_version 2+) classifies where a field came from.
    COMPLIANCE_EEOC/DEMOGRAPHIC/VOLUNTARY fields are never auto-answered
    (R-WT-8 8) and are marked `decline_allowed=True, required=False` at
    capture time.
    """

    STANDARD = "STANDARD"
    CUSTOM = "CUSTOM"
    LOCATION = "LOCATION"
    COMPLIANCE_EEOC = "COMPLIANCE_EEOC"
    DEMOGRAPHIC = "DEMOGRAPHIC"
    VOLUNTARY = "VOLUNTARY"


# Greenhouse field `type` string -> ARIA role for the a11y locator hint. The
# HTTP questions endpoint carries no DOM, so the locator is a best-effort role
# name that the (later) browser layer can reuse; the label is the accessible
# name. Unknown types fall back to a text box.
_ROLE_FOR_TYPE = {
    "input_text": "textbox",
    "input_file": "button",
    "textarea": "textbox",
    "multi_value_single_select": "combobox",
    "multi_value_multi_select": "listbox",
    "boolean": "checkbox",
    "yes_no": "combobox",
}


@dataclass
class Locator:
    role: str
    name: str


@dataclass
class Field:
    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    source: str
    locator: Locator
    step_index: int | None = None
    conditional_on: dict | None = None
    # -- W5 additive extension (schema_version 2): every new field defaults so
    # every existing construction site (the per-vendor capture modules, tests,
    # fixtures) keeps
    # working unchanged, and every v1-shaped cached FieldMap deserializes via
    # these same defaults (see `from_dict`).
    decline_allowed: bool = False
    max_length: int | None = None
    accept_types: list[str] | None = None
    norm_type: str = ""
    section: str = "STANDARD"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "options": list(self.options),
            "source": self.source,
            "locator": {"role": self.locator.role, "name": self.locator.name},
            "step_index": self.step_index,
            "conditional_on": self.conditional_on,
            "decline_allowed": self.decline_allowed,
            "max_length": self.max_length,
            "accept_types": (list(self.accept_types)
                            if self.accept_types is not None else None),
            "norm_type": self.norm_type,
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Field":
        """Reconstruct a `Field` from a dict of either shape.

        Tolerant by construction (`.get(key, <dataclass default>)` for every
        W5 field): a schema_version-1 cached row that never carried
        decline_allowed/max_length/accept_types/norm_type/section
        deserializes cleanly via these defaults, no store-side migration or
        version branch needed.
        """
        locator = data.get("locator") or {}
        accept_types = data.get("accept_types")
        raw_step = data.get("step_index")
        return cls(
            key=data["key"],
            label=data["label"],
            type=data["type"],
            required=bool(data["required"]),
            options=list(data.get("options") or []),
            source=data["source"],
            locator=Locator(role=locator.get("role", ""),
                            name=locator.get("name", "")),
            step_index=int(raw_step) if raw_step is not None else None,
            conditional_on=data.get("conditional_on"),
            decline_allowed=bool(data.get("decline_allowed", False)),
            max_length=data.get("max_length"),
            accept_types=(list(accept_types) if accept_types is not None
                         else None),
            norm_type=data.get("norm_type", ""),
            section=data.get("section", "STANDARD"),
        )


@dataclass
class FieldMap:
    vendor: str
    posting_id: str
    captured_at: str
    fields: list[Field] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "posting_id": self.posting_id,
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "fields": [f.to_dict() for f in self.fields],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FieldMap":
        return cls(
            vendor=data["vendor"],
            posting_id=str(data["posting_id"]),
            captured_at=data["captured_at"],
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            fields=[Field.from_dict(f) for f in data.get("fields", [])],
        )

    def required_fields(self) -> list[Field]:
        return [f for f in self.fields if f.required]

    def coverage(self, ssot: SSOT, profile: dict) -> "CoverageReport":
        # TRANSITIONAL call-time seam (dies in W5.1 Stage 3): the classifier
        # itself now lives in engine.kernel.resolve.coverage (vendor_resolver
        # injection seam, spec 3.4), but THIS method must keep delegating to
        # the engine.fieldmap.coverage SHIM because that shim default-injects
        # the Greenhouse widget resolver -- the remaining live method callers
        # (test_browse; run.py's two sites were moved onto the kernel function
        # with registry-built injection in Stage 2e-2) rely on today's
        # Greenhouse-widget classification. Stage 3 moves the remaining callers
        # onto the kernel function likewise; then this seam and
        # the `_KNOWN_UPWARD_EXCEPTIONS` allowlist entry in
        # tests/kernel/test_kernel_invariants.py are removed together. An eager
        # top-level import here would cycle back through fieldmap's own shim
        # import of this module.
        from engine.fieldmap import coverage as _coverage
        return _coverage(self, ssot, profile)


def _role_for_type(field_type: str) -> str:
    return _ROLE_FOR_TYPE.get(field_type, "textbox")


# == moved from engine.fill ==================================================

class FillSafetyError(RuntimeError):
    """A safety invariant of the dry run was about to be violated.

    Raised (never swallowed) when a click would hit a submit-like control, when
    the page navigated during the fill (possible submission/redirect), or when
    any other STOP-SHORT-OF-APPLYING guard trips. Distinct from a per-field fill
    error (which is fail-soft): a FillSafetyError aborts the whole fill.
    """


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


@dataclass
class FillAssets:
    """The whitelisted upload assets: the two CVs, the profile photo, and an
    optional cover-letter document.

    Every path is optional and runtime-verified: `verified()` drops any path
    that does not exist on disk to None, so an absent asset becomes a skip
    ("asset missing: <name>") rather than a crash (fail-soft, per the owner
    override). The upload whitelist is EXACTLY these resolved paths;
    `_safe_upload` refuses to upload anything else.
    """
    cv_ats: Path | None = None
    cv_atsi: Path | None = None
    photo: Path | None = None
    cover_letter: Path | None = None

    def verified(self) -> "FillAssets":
        """A copy whose non-existent asset paths are collapsed to None."""
        return FillAssets(cv_ats=_existing(self.cv_ats),
                          cv_atsi=_existing(self.cv_atsi),
                          photo=_existing(self.photo),
                          cover_letter=_existing(self.cover_letter))

    def is_whitelisted(self, path) -> bool:
        """True iff `path` resolves to one of the (existing) asset paths."""
        target = _resolved(path)
        if target is None:
            return False
        return any(_resolved(asset) == target
                   for asset in (self.cv_ats, self.cv_atsi, self.photo,
                                 self.cover_letter)
                   if asset is not None)


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


# == moved from engine.discover ==============================================

@dataclass
class Posting:
    vendor: str
    company_slug: str
    job_id: str
    title: str
    locations: list[str]
    remote_flag: bool
    comp: str | None
    posted_ts: str | None
    updated_ts: str | None
    url: str
    # Matching needs posting text; the spec's minimal field list plus this one
    # description field (fed by content=true / descriptionPlain) drives match.py.
    description: str = ""
    listed: bool = True
    unverified: bool = False
    # ToS-readable greenhouse fields (jobs/{id}?questions=true shape); safe
    # defaults so every existing construction site and the other adapters
    # (Lever/Ashby/Workable) keep working unchanged. Only GreenhouseAdapter
    # populates these today.
    departments: list[str] = field(default_factory=list)
    offices: list[str] = field(default_factory=list)
    requisition_id: str | None = None
    application_deadline: str | None = None
    company_name: str | None = None
    # W5.1: vendor-specific scrape/capture overflow (kernel stays vendor-
    # agnostic; a plugin stashes anything it needs here instead of the kernel
    # growing a vendor-shaped field). Must be `field(default_factory=dict)`,
    # not a bare `= {}` (dataclasses raise ValueError on a bare mutable default).
    vendor_extra: dict = field(default_factory=dict)

    def identity_key(self) -> str:
        """`company|role|url` per 7.4 (papers would key on DOI/arXiv instead)."""
        return f"{self.company_slug}|{self.title}|{self.url}"


class SourceAdapter(Protocol):
    vendor: str
    is_authoritative: bool

    def parse(self, raw, company_slug: str) -> list[Posting]:
        ...
