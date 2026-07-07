"""Ashby vendor plugin package (W5.1 Stage 2c).

Replaces the former single `engine.providers.ashby` MODULE. The Provider
contract surface (`vendor`, `capture`, `apply_url`, `resolve_values`, `fill`)
lives in `.fill` and is re-exported here so `import engine.providers.ashby as a`
keeps resolving every name the old module exposed, and `isinstance(a,
engine.providers.protocol.Provider)` still holds.

The vendor's other concern is split into a sibling submodule (reachable by its
canonical dotted path, `engine.providers.ashby.capture`):
  - `.fill`     -- fill() orchestration (own controlled-component select driver,
                   Turnstile checkbox/radio HUMAN HAND-OFF)
  - `.capture`  -- graphql schema capture/parse (moved out of `engine.browse`)

Ashby has NO `.resolve` submodule: `resolve_values` INHERITS greenhouse's
hole-fix e structural CV/photo choice (a load-bearing safety rule with ONE home
in `greenhouse.resolve`), so `.fill.resolve_values` just delegates there rather
than duplicating it -- exactly as workable does. The `.discover` adapter move
lands in Stage 2e WITH the eager-light `_registry.py`, because the CURRENT
`registry.py` loads at `engine.providers.__init__` time and from-imports the
adapter out of `engine.discover`; the adapter's plugin home is only reachable
THROUGH that same partially-initialized package (a load-time move here is a
proven hard import cycle).

NAME NOTE: `.capture` and `.fill` are submodules whose names collide with the
Provider callables `capture` / `fill`. At PACKAGE scope the callables win (the
`.fill` re-exports below run last), matching the old module where `ashby.capture`
/ `ashby.fill` were the functions. The submodules stay reachable via
`sys.modules` / `importlib.import_module`, which is exactly how the
`engine.browse` re-export shim reaches `.capture`'s moved members (never via the
package attribute).

`_apply_ashby_controlled` (the berellevy controlled-component select DRIVER) is
an INTERNAL `.fill` helper, but the ashby unit test asserts on it directly by
the package name (`ashby._apply_ashby_controlled`, the "naive value-set never
commits but the driver does" proof), so it is re-exported here to keep that seam
resolving at package scope. It is NOT in `__all__` (the Provider surface only).

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
from engine.providers.ashby import (  # noqa: F401
    capture as _capture_mod,
)
from engine.providers.ashby.fill import (  # noqa: F401
    _apply_ashby_controlled,
    apply_url,
    capture,
    fill,
    resolve_values,
    vendor,
)

__all__ = ["vendor", "capture", "apply_url", "resolve_values", "fill"]
