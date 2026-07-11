"""Shared generic capture-support: vendor-native type mapping + I/O helpers.

The slim, vendor-agnostic support module every ATS capture plugin builds on. It
owns exactly one concern: the generic core that maps a vendor-native `type`
string onto the canonical `FieldType` (`normalize_type`, `_TYPE_MAP`,
`_HIDDEN_TYPES`) plus two capture I/O helpers (`_read_body_text`,
`_utc_now_iso`). Nothing here is vendor-specific.

RATIFICATION (owner 2026-07-10): this module STAYS. Its generic core
(`normalize_type`, `_TYPE_MAP`, `_HIDDEN_TYPES`, `_read_body_text`,
`_utc_now_iso`) is kernel-tier shared logic that the vendor capture plugins
(`engine.providers.{greenhouse,workable}.capture`) IMPORT and never co-edit;
there is exactly one definition of each name, here.

History: in Stage 5 the transitional re-export shims dissolved. The
contracts/resolve name re-exports, the greenhouse-default `coverage()` shim, and
the lazy PEP 562 vendor `__getattr__` routes were all removed once every importer
moved onto the canonical homes (`engine.kernel.contracts`,
`engine.kernel.resolve`, the per-vendor capture/resolve modules).
"""

from __future__ import annotations


from engine.kernel.contracts import FieldType

# Vendor-native `type` string -> canonical FieldType. Covers the Greenhouse
# HTTP-schema vocabulary (also what Lever's DOM controls and Ashby's own
# _ASHBY_TYPE_MAP shim collapse into today, in engine/providers/ashby/capture.py)
# PLUS the raw Ashby ApiJobPosting type strings, so a future provider can call
# `normalize_type` directly on either vocabulary without drift.
_TYPE_MAP = {
    # Greenhouse HTTP schema / Lever DOM / post-collapse Ashby.
    "input_text": FieldType.TEXT,
    "textarea": FieldType.LONGTEXT,
    "multi_value_single_select": FieldType.SINGLE_SELECT,
    "multi_value_multi_select": FieldType.MULTI_SELECT,
    "boolean": FieldType.BOOLEAN,
    "input_file": FieldType.FILE,
    # Raw Ashby ApiJobPosting field `type` (pre-collapse).
    "String": FieldType.TEXT,
    "Email": FieldType.EMAIL,
    "LongText": FieldType.LONGTEXT,
    "ValueSelect": FieldType.SINGLE_SELECT,
    "MultiValueSelect": FieldType.MULTI_SELECT,
    "Phone": FieldType.PHONE,
    "Date": FieldType.DATE,
    "Boolean": FieldType.BOOLEAN,
    "File": FieldType.FILE,
    "Number": FieldType.NUMBER,
}

# Greenhouse tracking/hidden fields (`input_hidden`) carry no user-facing
# control at all: never a fillable Field, so `normalize_type` returns "" for
# them and capture skips creating a Field for the sub-field entirely.
_HIDDEN_TYPES = frozenset({"input_hidden"})


def normalize_type(vendor_native: str) -> str:
    """Map a vendor-native `type` string onto the canonical `FieldType`.

    Reused across every capture path (Greenhouse HTTP schema, the raw Ashby
    ApiJobPosting type before its collapse in engine/providers/ashby/capture.py,
    Lever's DOM-derived types which already share the Greenhouse vocabulary) so
    downstream consumers reason about ONE type system. `input_hidden` has no
    user-facing control and returns "" (skip signal, not a `FieldType`
    member); an unrecognised native falls back to `FieldType.TEXT` (mirrors
    `_role_for_type`'s fallback-to-textbox convention).
    """
    key = vendor_native or ""
    if key in _HIDDEN_TYPES:
        return ""
    return _TYPE_MAP.get(key, FieldType.TEXT)


def _read_body_text(response) -> str:
    body = response.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body


