"""Optional/required extra-attachment plumbing (W5.1a Stage 5, wave S5d).

Owner rule (2026-07-10): when an ATS posting exposes a slot for a named extra
document (transcript, certification), we ALWAYS attach the matching one -- the
slot being optional or required makes no difference. This lands in the kernel
before the four vendor loops fork, and is the formal successor of the deleted
`engine.fill.default_assets` asset-resolution role.

These tests bite on outcomes (the resolved FieldValue's asset/path, the skip
set, the completeness accounting), never on mere non-crash. Built directly on
the kernel contracts (no vendor code), mirroring `tests/kernel/test_cv_rule.py`:
minimal FieldMaps of upload fields resolved through
`engine.kernel.resolve.resolve_values`, with real on-disk `tmp_path` stub files
so `FillAssets.verified()` keeps every leg.
"""

from __future__ import annotations

from engine.kernel.contracts import Field, FieldMap, FillAssets, Locator
from engine.kernel.resolve import _completeness, resolve_values
from engine.kernel.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"


# --- minimal kernel-contract fixture builders --------------------------------

def _upload_field(key: str, label: str, *, required: bool = True) -> Field:
    """An `input_file` upload control (the attachment rule only sees uploads)."""
    return Field(key=key, label=label, type="input_file", required=required,
                 options=[], source="questions",
                 locator=Locator(role="button", name=label))


def _fieldmap(*fields: Field) -> FieldMap:
    return FieldMap(vendor="greenhouse", posting_id="1", captured_at=_PINNED,
                    fields=list(fields))


def _assets(tmp_path, *, extras=("lse_certification", "transcript_university",
                                 "transcript_ib")) -> FillAssets:
    """Real on-disk stub assets (both CVs + the named extra documents) so
    `verified()` keeps every leg. `extras=()` builds assets with NO extra
    documents (a CV still present, so the CV fallthrough stays exercisable)."""
    (tmp_path / "cv-ats.pdf").write_bytes(b"stub")
    (tmp_path / "cv-atsi.pdf").write_bytes(b"stub")
    extra_documents: dict = {}
    for key in extras:
        path = tmp_path / f"{key}.pdf"
        path.write_bytes(b"stub")
        extra_documents[key] = path
    return FillAssets(cv_ats=tmp_path / "cv-ats.pdf",
                      cv_atsi=tmp_path / "cv-atsi.pdf",
                      extra_documents=extra_documents)


def _resolve_one(tmp_path, field: Field, assets: FillAssets):
    resolved = resolve_values(_fieldmap(field), SSOT({}), {}, assets=assets)
    return resolved, {fv.key: fv for fv in resolved.fields}


# --- the 9 wave-S5d tests -----------------------------------------------------

def test_extra_documents_verified_drops_missing_paths(tmp_path) -> None:
    """`verified()` keeps an extra_documents entry whose file exists and DROPS
    (key and all) an entry whose path is missing on disk."""
    present = tmp_path / "transcript-ib.pdf"
    present.write_bytes(b"stub")
    missing = tmp_path / "does-not-exist.pdf"          # never created
    verified = FillAssets(extra_documents={
        "transcript_ib": present,
        "transcript_university": missing,
    }).verified()
    assert "transcript_ib" in verified.extra_documents
    assert verified.extra_documents["transcript_ib"] == present
    assert "transcript_university" not in verified.extra_documents


def test_certification_label_matches_lse_certification(tmp_path) -> None:
    """An upload field labelled as a certification resolves to the
    `lse_certification` extra document."""
    assets = _assets(tmp_path)
    _resolved, by_key = _resolve_one(
        tmp_path, _upload_field("certification", "Certification"), assets)
    assert "certification" in by_key
    fv = by_key["certification"]
    assert fv.asset == "lse_certification"
    assert fv.value == assets.extra_documents["lse_certification"]


def test_transcript_label_matches_university_transcript(tmp_path) -> None:
    """A generic transcript label resolves to the `transcript_university`
    extra document."""
    assets = _assets(tmp_path)
    _resolved, by_key = _resolve_one(
        tmp_path, _upload_field("transcript", "Academic Transcript"), assets)
    fv = by_key["transcript"]
    assert fv.asset == "transcript_university"
    assert fv.value == assets.extra_documents["transcript_university"]


def test_ib_transcript_label_beats_generic_transcript(tmp_path) -> None:
    """An IB-transcript label matches `transcript_ib`, NOT the generic
    `transcript_university`: the IB patterns are ordered first so the more
    specific document wins (the ordering lock)."""
    assets = _assets(tmp_path)
    _resolved, by_key = _resolve_one(
        tmp_path, _upload_field("ib_transcript", "IB Transcript"), assets)
    fv = by_key["ib_transcript"]
    assert fv.asset == "transcript_ib"
    assert fv.asset != "transcript_university"
    assert fv.value == assets.extra_documents["transcript_ib"]


def test_ib_transcript_key_only_label_absent_still_resolves_transcript_ib(
        tmp_path) -> None:
    """The separator-folding-on-KEY branch of `_match_extra_document` in
    isolation: with an EMPTY label the `ib_transcript` key alone must fold to
    "ib transcript" and resolve `transcript_ib`, beating the generic
    `transcript_university` (which the bare key would also substring-match).
    This proves the key-only match path the docstring claims, independent of
    any label substring."""
    assets = _assets(tmp_path)
    _resolved, by_key = _resolve_one(
        tmp_path, _upload_field("ib_transcript", ""), assets)
    fv = by_key["ib_transcript"]
    assert fv.asset == "transcript_ib"                  # matched on key alone
    assert fv.asset != "transcript_university"          # ordering lock holds
    assert fv.value == assets.extra_documents["transcript_ib"]


def test_unmatched_upload_label_keeps_existing_behavior(tmp_path) -> None:
    """An upload field naming no known extra document (and not a
    photo/cover-letter) keeps its EXISTING behavior: the CV rule fills it (a
    plain non-photo form -> cv-atsi), never an extra document."""
    assets = _assets(tmp_path)
    _resolved, by_key = _resolve_one(
        tmp_path, _upload_field("writing_sample", "Writing sample"), assets)
    fv = by_key["writing_sample"]
    assert fv.asset == "cv-atsi"                       # existing CV fallthrough
    assert fv.asset not in assets.extra_documents      # not stolen by a matcher


def test_matched_attachment_resolves_to_upload_fieldvalue(tmp_path) -> None:
    """A matched attachment resolves to an upload FieldValue carrying the
    extra document's key, exact path, and file type, is FILLED (not skipped),
    and is UPLOADABLE through the shared provider-side single-asset whitelist
    reconstruction (proven here for an extra-document slot and the cover-letter
    slot, and rejected for an empty asset name). (The resolve half is the bite
    proven by breaking the matcher wiring.)"""
    assets = _assets(tmp_path)
    resolved, by_key = _resolve_one(
        tmp_path, _upload_field("transcript", "University Transcript"), assets)
    assert "transcript" in by_key                      # filled, not skipped
    assert "transcript" not in dict(resolved.skipped)
    fv = by_key["transcript"]
    assert fv.asset == "transcript_university"
    assert fv.value == assets.extra_documents["transcript_university"]
    assert fv.type == "input_file"
    # fill_form receives only the resolved FieldValues (not the original
    # FillAssets), so every vendor rebuilds a single-asset whitelist from
    # fv.asset/fv.value via FillAssets.single_asset_whitelist and feeds it to
    # _safe_upload. Follow the matched attachment through that reconstruction
    # and confirm the path is whitelisted (else _safe_upload would abort).
    assert FillAssets.single_asset_whitelist(fv.asset, fv.value).is_whitelisted(
        fv.value)
    # The kernel emits a resolved cover-letter FieldValue provider-agnostically,
    # so the same reconstruction MUST whitelist a "cover-letter" asset on every
    # vendor path (greenhouse/lever/ashby/workable). Were it not whitelisted,
    # _safe_upload would raise FillSafetyError and abort the entire fill.
    cover = tmp_path / "cover-letter.pdf"
    cover.write_bytes(b"stub")
    assert FillAssets.single_asset_whitelist(
        "cover-letter", cover).is_whitelisted(cover)
    # Safe-fail teeth: an empty/None asset name yields an EMPTY whitelist, so an
    # unnamed asset is never admitted by the reconstruction.
    assert not FillAssets.single_asset_whitelist("", cover).is_whitelisted(cover)


def test_attachment_key_missing_from_assets_fails_soft(tmp_path) -> None:
    """A matched label whose key is ABSENT from the assets falls back to the
    existing CV behavior (fail-soft, no crash), never to the missing extra
    document."""
    assets = _assets(tmp_path, extras=())              # no extra documents
    _resolved, by_key = _resolve_one(
        tmp_path, _upload_field("transcript", "University Transcript"), assets)
    fv = by_key["transcript"]
    assert fv.asset == "cv-atsi"                        # existing CV fallthrough
    assert fv.asset != "transcript_university"


def test_attached_extra_document_counts_as_filled_in_completeness(
        tmp_path) -> None:
    """An attached extra document counts as FILLED in completeness exactly like
    any other upload: the required attachment field is in the denominator and
    NOT in `required_unfilled`."""
    assets = _assets(tmp_path)
    field = _upload_field("transcript", "University Transcript", required=True)
    fmap = _fieldmap(field)
    resolved = resolve_values(fmap, SSOT({}), {}, assets=assets)
    filled_keys = {fv.key for fv in resolved.fields}
    assert "transcript" in filled_keys                 # it resolved to a fill
    fillable_total, required_unfilled, _justified = _completeness(
        fmap, filled_keys, list(resolved.skipped), len(filled_keys))
    assert fillable_total == 1
    assert required_unfilled == []
    assert "transcript" not in {r["key"] for r in required_unfilled}
