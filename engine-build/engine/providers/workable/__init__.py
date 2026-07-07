"""Workable vendor plugin package (W5.1 Stage 2b).

Replaces the former single `engine.providers.workable` MODULE. The Provider
contract surface (`vendor`, `capture`, `apply_url`, `resolve_values`, `fill`)
lives in `.fill` and is re-exported here so `import engine.providers.workable as
w` keeps resolving every name the old module exposed, and `isinstance(w,
engine.providers.protocol.Provider)` still holds.

The vendor's other concern is split into a sibling submodule (reachable by its
canonical dotted path, `engine.providers.workable.capture`):
  - `.fill`     -- fill() orchestration (native DOM path, wide Turnstile hand-off)
  - `.capture`  -- schema capture/parse (moved out of `engine.fieldmap`)

Workable has NO `.resolve` submodule: `resolve_values` INHERITS greenhouse's
hole-fix and structural CV/photo choice (a load-bearing safety rule with ONE home
in `greenhouse.resolve`), so `.fill.resolve_values` just delegates there rather
than duplicating it. The `.discover` adapter move lands in Stage 2e WITH the
eager-light `_registry.py`, because the CURRENT `registry.py` loads at
`engine.providers.__init__` time and from-imports the adapter out of
`engine.discover`; the adapter's plugin home is only reachable THROUGH that same
partially-initialized package (a load-time move here is a proven hard import
cycle).

NAME NOTE: `.capture` and `.fill` are submodules whose names collide with the
Provider callables `capture` / `fill`. At PACKAGE scope the callables win (the
`.fill` re-exports below run last), matching the old module where
`workable.capture` / `workable.fill` were the functions. The submodules stay
reachable via `sys.modules` / `importlib.import_module`, which is exactly how the
`engine.fieldmap` re-export shim reaches `.capture`'s moved members (never via
the package attribute).

Kept LIGHT (matching the old module's import cost): importing this package loads
NO patchright / `engine.browse`; the fill body still reaches `engine.fill`'s
private helpers via CALL-TIME imports. This package does NOT self-register with
any registry yet (Stage 2e wires the new registry).
"""

# Eager-load the sibling submodule so the whole vendor namespace is populated
# when the package is imported (matches the old all-in-one module). Aliased to
# an underscore name so it does NOT shadow the Provider callables re-exported
# below; the submodule remains reachable by its canonical dotted path.
from engine.providers.workable import (  # noqa: F401
    capture as _capture_mod,
)
from engine.providers.workable.fill import (  # noqa: F401
    apply_url,
    capture,
    fill,
    resolve_values,
    vendor,
)

# -- Stage 2e self-registration into the eager-light auto-registry -------------
# The board-JSON adapter is a LEAF module (`engine.kernel.*` only); it is the one
# name this package needs from `.discover` and is safe to import at load.
from engine.providers.workable.discover import WorkableAdapter  # noqa: E402,F401


def vendor_resolver():
    """Workable has no portal-widget resolver (native DOM path, wide Turnstile
    hand-off); the kernel classifier consults none for this vendor."""
    return None


# `_registry` defines `register` before importing the plugins, so this resolves
# even under re-entrant self-registration. `capture`/`fill` are LAZY (resolved on
# call) to keep both the registry and this import browser-free; `resolve_values`
# is the package's own callable (it delegates greenhouse's hole-fix per the
# module docstring).
from engine.providers import _registry  # noqa: E402

_registry.register(
    vendor=vendor,
    capture=_registry.lazy_call("engine.providers.workable", "capture"),
    fill=_registry.lazy_call("engine.providers.workable", "fill"),
    apply_url=apply_url,
    resolve_values=resolve_values,
    adapter=WorkableAdapter,
    vendor_resolver=vendor_resolver,
)

__all__ = ["vendor", "capture", "apply_url", "resolve_values", "fill",
           "vendor_resolver"]
