"""Lever vendor plugin package (W5.1 Stage 2d).

Replaces the former single `engine.providers.lever` MODULE. The Provider
contract surface (`vendor`, `capture`, `apply_url`, `resolve_values`, `fill`)
lives in `.fill` and is re-exported here so `import engine.providers.lever as l`
keeps resolving every name the old module exposed, and `isinstance(l,
engine.providers.protocol.Provider)` still holds.

The vendor's other concern is split into a sibling submodule (reachable by its
canonical dotted path, `engine.providers.lever.capture`):
  - `.fill`     -- fill() orchestration (NATIVE select / type_human driver, no
                   react-select; checkbox/radio hCaptcha HUMAN HAND-OFF)
  - `.capture`  -- server-rendered apply-DOM capture/parse (moved out of
                   `engine.browse`)

Lever has NO `.resolve` submodule: `.fill.resolve_values` delegates to the
kernel's `engine.kernel.resolve.resolve_values`, which carries the hole-fix e
structural CV/photo choice (a load-bearing safety rule single-sourced in the
kernel), so no vendor duplicates it -- exactly as ashby / workable do. The `.discover`
adapter move lands in Stage 2e WITH the eager-light `_registry.py`, because the
CURRENT `registry.py` loads at `engine.providers.__init__` time and from-imports
the adapter out of `engine.discover`; the adapter's plugin home is only reachable
THROUGH that same partially-initialized package (a load-time move here is a
proven hard import cycle).

NAME NOTE: `.capture` and `.fill` are submodules whose names collide with the
Provider callables `capture` / `fill`. At PACKAGE scope the callables win (the
`.fill` re-exports below run last), matching the old module where `lever.capture`
/ `lever.fill` were the functions. The submodules stay reachable via
`sys.modules` / `importlib.import_module`, which is exactly how the
`engine.browse` re-export shim reaches `.capture`'s moved members (never via the
package attribute).

Kept LIGHT (matching the old module's import cost): importing this package loads
NO patchright / `engine.browse`; the fill/capture bodies still reach
`engine.fill`'s private helpers via CALL-TIME imports, and patchright loads
lazily in the kernel only when a real capture runs. This package does NOT
self-register with any registry yet (Stage 2e wires the new registry).
"""

# Eager-load the sibling submodule so the whole vendor namespace is populated
# when the package is imported (matches the old all-in-one module). Aliased to
# an underscore name so it does NOT shadow the Provider callables re-exported
# below; the submodule remains reachable by its canonical dotted path.
from engine.providers.lever import (  # noqa: F401
    capture as _capture_mod,
)
from engine.providers.lever.fill import (  # noqa: F401
    apply_url,
    capture,
    fill,
    resolve_values,
    vendor,
)

# -- Stage 2e self-registration into the eager-light auto-registry -------------
# The board-JSON adapter + board poll-URL builder are LEAF names (`engine.kernel.*`
# only); they are what this package needs from `.discover` and are safe to import
# at load.
from engine.providers.lever.discover import (  # noqa: E402,F401
    LeverAdapter,
    lever_endpoint,
)


def vendor_resolver():
    """Lever has no portal-widget resolver (native selects / server-rendered
    apply DOM); the kernel classifier consults none for this vendor."""
    return None


# `_registry` defines `register` before importing the plugins, so this resolves
# even under re-entrant self-registration. `capture`/`fill` are LAZY (resolved on
# call) to keep both the registry and this import browser-free; `resolve_values`
# is the package's own callable (it delegates greenhouse's hole-fix per the
# module docstring).
from engine.providers import _registry  # noqa: E402

_registry.register(
    vendor=vendor,
    capture=_registry.lazy_call("engine.providers.lever", "capture"),
    fill=_registry.lazy_call("engine.providers.lever", "fill"),
    apply_url=apply_url,
    resolve_values=resolve_values,
    adapter=LeverAdapter,
    vendor_resolver=vendor_resolver,
    endpoint_fn=lever_endpoint,
    hosts=("lever.co",),
)

__all__ = ["vendor", "capture", "apply_url", "resolve_values", "fill",
           "vendor_resolver"]
