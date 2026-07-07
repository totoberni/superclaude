"""Owner-ratified CV/photo rule, locked as a STANDING invariant (2026-07-07).

THE RULE (no exceptions, posting-language IRRELEVANT):
  - the form has NO photo field  -> resume CV asset is `cv-atsi` (the ATSI
    variant embeds the portrait, since there is nowhere else to carry it);
  - the form HAS a photo field    -> resume CV asset is `cv-ats` (the plain ATS
    CV; the portrait attaches to the dedicated photo field, which itself
    resolves to the `photo` asset).

This flipped an earlier posting-language-driven `_select_cv` branch (the
Italian-vs-English gate). This test is the regression guard that the outcome is
purely form-structural: the `it` and `en` rows MUST produce identical CV assets
in every case. If a future edit re-introduced a posting-language dependency in
`engine.kernel.resolve._select_cv`, the language-invariance rows below would
diverge and fail.

Built directly on the kernel contracts (no vendor code): minimal FieldMaps of
upload fields resolved through `engine.kernel.resolve.resolve_values`. A photo
field is an `input_file` whose label reads as a portrait ("Profile picture");
the resume field is an `input_file` labelled "Resume".
"""

from __future__ import annotations

import pytest

from engine.kernel.contracts import Field, FieldMap, FillAssets, Locator
from engine.kernel.resolve import resolve_values
from engine.kernel.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"


# --- minimal kernel-contract fixture builders --------------------------------

def _upload_field(key: str, label: str) -> Field:
    """An `input_file` upload control (the CV/photo rule only ever sees uploads)."""
    return Field(key=key, label=label, type="input_file", required=True,
                 options=[], source="questions",
                 locator=Locator(role="button", name=label))


def _fieldmap(*fields: Field) -> FieldMap:
    return FieldMap(vendor="greenhouse", posting_id="1", captured_at=_PINNED,
                    fields=list(fields))


def _assets(tmp_path) -> FillAssets:
    """Real on-disk stub assets so `FillAssets.verified()` keeps every leg.

    Every path must exist, else `verified()` collapses it to None and the
    upload skips as "asset missing" instead of exercising the CV rule.
    """
    for name in ("cv-ats.pdf", "cv-atsi.pdf", "Me.png"):
        (tmp_path / name).write_bytes(b"stub")
    return FillAssets(cv_ats=tmp_path / "cv-ats.pdf",
                      cv_atsi=tmp_path / "cv-atsi.pdf",
                      photo=tmp_path / "Me.png")


def _resolve(tmp_path, *, photo_present: bool, posting_lang: str):
    fields = [_upload_field("resume", "Resume")]
    if photo_present:
        # A portrait-labelled input_file is the form's photo field.
        fields.append(_upload_field("photo", "Profile picture"))
    resolved = resolve_values(_fieldmap(*fields), SSOT({}), {},
                              assets=_assets(tmp_path), posting_lang=posting_lang)
    return {fv.key: fv for fv in resolved.fields}


# --- the rule, across all four (posting_lang x photo) combinations ------------

@pytest.mark.parametrize("posting_lang", ["it", "en"])
@pytest.mark.parametrize("photo_present", [True, False],
                         ids=["photo-present", "photo-absent"])
def test_cv_asset_is_form_structural_not_language_driven(
        tmp_path, posting_lang: str, photo_present: bool) -> None:
    """resume asset = cv-ats iff a photo field is present, else cv-atsi -- for
    BOTH `it` and `en` postings (posting language is irrelevant)."""
    by_key = _resolve(tmp_path, photo_present=photo_present,
                      posting_lang=posting_lang)
    resume = by_key["resume"]
    if photo_present:
        assert resume.asset == "cv-ats"
        assert "photo field present" in (resume.upload_reason or "")
        # The dedicated photo field carries the portrait itself.
        assert by_key["photo"].asset == "photo"
    else:
        assert resume.asset == "cv-atsi"
        assert "no photo field" in (resume.upload_reason or "")


@pytest.mark.parametrize("photo_present", [True, False],
                         ids=["photo-present", "photo-absent"])
def test_posting_language_does_not_change_cv_asset(
        tmp_path, photo_present: bool) -> None:
    """Language-independence guard: the `it` and `en` rows produce the IDENTICAL
    CV asset. This is the explicit lock against a future regression
    re-introducing the posting-language dependency in `_select_cv`."""
    it_asset = _resolve(tmp_path, photo_present=photo_present,
                        posting_lang="it")["resume"].asset
    en_asset = _resolve(tmp_path, photo_present=photo_present,
                        posting_lang="en")["resume"].asset
    assert it_asset == en_asset, (
        "posting language changed the CV asset (it != en): the owner-ratified "
        "rule is purely form-structural and MUST be posting-language independent")
    assert it_asset == ("cv-ats" if photo_present else "cv-atsi")
