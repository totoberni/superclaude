"""The Provider CONTRACT every per-vendor ATS module implements (W5.2).

`greenhouse.py` is the FIRST reference implementation; `lever.py` and
`ashby.py` (W5.3) and `workable.py` (W5.4) follow the same shape. Centralizing
the contract here (rather than letting each provider module invent its own)
is the DRY/separation-of-concerns goal the W5 spec names explicitly (section
1): the ONLY vendor-specific code is what lives inside a provider's `fill()`
body driving the concrete DOM; everything else -- schema fetch, apply-URL
resolution, the never-send interceptor, readback gating, DOM-sweep
completeness -- is either registry-level or `providers.base`-level and shared
by construction.

A conforming provider:
- `vendor`: the canonical vendor key (matches its `_registry.ProviderSpec`).
- `capture(slug, job_id, opener=None) -> FieldMap`: the schema fetch. Every
  reference implementation delegates this to `_registry.resolve(vendor).
  capture` (the registry already owns the vendor -> capture-function
  wiring; a provider module never re-derives it).
- `apply_url(slug, job_id) -> str`: the public apply-page URL. Same
  delegation pattern via `_registry.resolve(vendor).apply_url`.
- `fill(page, fieldmap, values, *, dry_run=True) -> FillReport`: drives an
  ALREADY-NAVIGATED page. Every implementation's ordering is fixed by the W5
  spec's hole-fixes (section 5) and is not a per-provider choice:
    1. `base.install_never_send(page)` FIRST -- the structural network-layer
       never-send backstop (hole-fix a), installed before any field is
       touched so no interaction can race ahead of it.
    2. drive each `values.fields` entry through the shared `base.py`
       primitives (`base.type_human`, `base.select_react_combobox`, the
       `base._safe_upload` re-export) -- never a raw `locator.fill()` /
       arbitrary click.
    3. readback-gate every field via `base._readback` (or the react-select
       driver's own poll): a value only counts toward `FillReport.filled`
       once the page confirms it landed (preserves the W4 honest caption).
    4. cross-check `base.sweep_required(page)` against the fieldmap's
       required-field set via `base.completeness_mismatch`; ANY mismatch
       (either direction) forces the report to `NOT_COMPLETE` (hole-fix d).
    5. return a `FillReport` (the existing `engine.kernel.contracts.FillReport`
       dataclass -- providers never invent a parallel report shape).
  `dry_run` is accepted for interface stability; Part 1 carries no submit
  code path regardless of its value (`install_never_send` is unconditional),
  so a provider stub is not obligated to branch on it in this wave.

`Provider` is a `typing.Protocol` (structural, not a base class): a provider
module satisfies it by exposing these three callables at module scope (the
reference pattern: free functions at package scope, as every vendor plugin
package does) or on a lightweight class -- either shape type-checks against
the Protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # FieldMap now lives in the kernel (W5.1 stage 0), so this annotation-only
    # import is kernel-internal -- the pre-move fetch -> providers -> fieldmap
    # -> fetch cycle rationale for deferring it no longer applies to this path.
    # It stays TYPE_CHECKING-gated purely to keep protocol.py's load graph
    # minimal (`from __future__ import annotations` makes every annotation a
    # lazy string; nothing here needs the class at runtime).
    from engine.kernel.contracts import FieldMap


@runtime_checkable
class Provider(Protocol):
    """Structural contract shared by every ATS provider module (greenhouse,
    lever, ashby, workable). See the module docstring for the full sequencing
    rules `fill()` implementations must honour."""

    vendor: str

    def capture(self, slug: str, job_id: str, opener: Any = None) -> FieldMap:
        """The schema fetch; every reference implementation delegates to the
        registry's `capture` for this vendor."""
        ...

    def apply_url(self, slug: str, job_id: str) -> str:
        """The public apply-page URL; delegates to the registry's
        `apply_url` for this vendor."""
        ...

    def fill(self, page: Any, fieldmap: FieldMap, values: Any,
             *, dry_run: bool = True) -> Any:
        """Drive an already-navigated `page` per the ordering documented on
        this Protocol; returns an `engine.kernel.contracts.FillReport`."""
        ...
