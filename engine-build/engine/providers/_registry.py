"""Eager-but-light provider auto-registry (W5.1 Stage 2e-1; spec section 6).

The Stage-3 successor to `engine.providers.registry`: a single `vendor ->
ProviderSpec` map that plugins populate by SELF-REGISTERING on import, instead of
one central module reaching out to every vendor. This module defines ONLY the
mechanism -- `register()` and the small `ProviderSpec` it stores -- and imports
NO plugin itself.

POPULATION (why this module does not import the plugins). Each vendor package
self-registers from its own `__init__` (`_registry.register(...)`). Those
`__init__`s are already imported eagerly, and for ALL four vendors, by
`engine.providers.registry` and `engine.discover`, each of which imports every
`engine.providers.<vendor>.discover` leaf (which forces the vendor package
`__init__` to run). Since importing any `engine.providers.*` name first runs the
package `engine.providers.__init__` -> `engine.providers.registry`, that transit
loads and self-registers all four vendors; by the time control returns from
`import engine.providers._registry`, `PROVIDERS` is fully populated.

Crucially this module does NOT import the plugins at its own bottom. It would be
the classic re-entrant self-registration pattern -- but one vendor's `__init__`
is reachable (via `engine.fieldmap`'s lazy `__getattr__` shim for
`GREENHOUSE_WIDGET_RESOLVER`) WHILE `engine.fill` is still mid-import; a cascade
from that vendor into the OTHER vendors' `.fill` (which import their dataclasses
from `engine.fill`, unlike greenhouse which reads `engine.kernel.contracts`) then
re-enters the half-initialised `engine.fill` and raises. Letting each vendor
register itself in isolation -- with no cross-plugin cascade from `register()` --
sidesteps that entirely.

EAGER: after any `engine.providers.*` import, `PROVIDERS` holds all four vendors.
LIGHT: no browser module is loaded. `capture`/`fill` are registered as LAZY
path-based callables (`lazy_call`, below) that import + invoke the plugin function
only when CALLED, so (1) a monkeypatched plugin function is honoured at call time
and (2) importing the registry never forces a browser driver. This module imports
NO pipeline module (`engine.discover`, `engine.fieldmap`, `engine.browse`,
`engine.fill`).

Field parity with `engine.providers.registry.ProviderSpec` is deliberate so the
Stage-3 migration of the old registry's callers onto this map is mechanical.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the engine needs to talk to one ATS vendor, in one place.

    Fields (parallel to `engine.providers.registry.ProviderSpec` for a mechanical
    Stage-3 migration):
        vendor          canonical vendor key ("greenhouse" | "lever" | ...).
        capture         field-map capture callable, (slug, job_id, opener=None)
                        -> FieldMap. LAZY (`lazy_call`): resolves the plugin
                        function at call time.
        fill            fill() driver, (page, fieldmap, values, ...) -> FillReport.
                        LAZY (`lazy_call`).
        apply_url       public apply-page URL builder, (slug, job_id) -> str
                        (browser-free; bound directly, not lazy).
        resolve_values  value-resolution callable (the vendor's own; the browser
                        vendors' delegates greenhouse's hole-fix per their plugin
                        docstrings). Bound directly.
        adapter         the vendor's board-JSON discover adapter CLASS (from
                        `engine.providers.<vendor>.discover`); None for a stub.
        vendor_resolver zero-arg callable returning the vendor's portal-widget
                        resolver object, or None when the vendor has none.
        supported       False for a documented stub; excluded from live wiring.
    """

    vendor: str
    capture: Callable[..., object]
    fill: Callable[..., object]
    apply_url: Callable[..., str]
    resolve_values: Callable[..., object]
    adapter: type | None
    vendor_resolver: Callable[[], object] | None = None
    supported: bool = True


PROVIDERS: dict[str, ProviderSpec] = {}


def register(vendor: str, *, capture: Callable[..., object],
             fill: Callable[..., object], apply_url: Callable[..., str],
             resolve_values: Callable[..., object], adapter: type | None,
             vendor_resolver: Callable[[], object] | None = None,
             supported: bool = True) -> ProviderSpec:
    """Record (or REPLACE) the wiring for one vendor.

    Idempotent by REPLACE: `PROVIDERS` is keyed on `vendor`, so registering the
    same vendor twice overwrites the prior entry rather than raising or
    duplicating. That keeps the re-entrant self-registration path (a plugin can
    be reached, and thus re-import, from more than one edge of the load graph)
    safe -- the last write wins and the map holds exactly one spec per vendor.
    Returns the stored spec for the caller's convenience.
    """
    spec = ProviderSpec(vendor=vendor, capture=capture, fill=fill,
                        apply_url=apply_url, resolve_values=resolve_values,
                        adapter=adapter, vendor_resolver=vendor_resolver,
                        supported=supported)
    PROVIDERS[vendor] = spec
    return spec


def all_providers() -> tuple[str, ...]:
    """The registered vendor keys, in registration order."""
    return tuple(PROVIDERS)


def get(vendor: str) -> ProviderSpec:
    """The `ProviderSpec` for `vendor`; raises `KeyError` if unregistered."""
    return PROVIDERS[vendor]


def lazy_call(module_path: str, attr: str) -> Callable[..., object]:
    """Build a named (lambda-free) CALL-TIME resolver for `module_path:attr`.

    On each call it imports `module_path` and invokes its `attr`, so a
    monkeypatched plugin function is honoured (the reference is looked up on the
    module object at call time, never bound at registration), and importing this
    registry never forces the plugin's heavy code path. The returned function
    carries a `_target` tuple for introspection/tests.
    """
    def _call(*args, **kwargs):
        module = importlib.import_module(module_path)
        return getattr(module, attr)(*args, **kwargs)

    _call.__name__ = f"lazy_{attr}"
    _call.__qualname__ = f"lazy_call.<{module_path}:{attr}>"
    _call._target = (module_path, attr)
    return _call
