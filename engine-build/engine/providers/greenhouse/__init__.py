"""Greenhouse vendor plugin package (W5.1 Stage 2a).

Replaces the former single `engine.providers.greenhouse` MODULE. The Provider
contract surface (`vendor`, `capture`, `apply_url`, `resolve_values`, `fill`)
lives in `.fill` and is re-exported here so `import engine.providers.greenhouse
as g` keeps resolving every name the old module exposed, and
`isinstance(g, engine.providers.protocol.Provider)` still holds.

The vendor's other concerns are split into sibling submodules (all reachable by
their canonical dotted path, e.g. `engine.providers.greenhouse.capture`):
  - `.fill`     -- fill() orchestration + the react-select / upload widget cluster
  - `.capture`  -- schema capture/parse (moved out of `engine.fieldmap`)
  - `.resolve`  -- portal-widget coverage resolver (moved out of `engine.fieldmap`)
  - `.discover` -- lands in Stage 2e: the board-JSON adapter move is sequenced
                   WITH the eager-light `_registry.py`, because the CURRENT
                   `registry.py` loads at `engine.providers.__init__` time and
                   from-imports the adapter out of `engine.discover`; the
                   adapter's plugin home is only reachable THROUGH that same
                   partially-initialized package (a load-time move here is a
                   proven hard import cycle).

NAME NOTE: `.capture` and `.fill` are submodules whose names collide with the
Provider callables `capture` / `fill`. At PACKAGE scope the callables win (the
`.fill` re-exports below run last), matching the old module where
`greenhouse.capture` / `greenhouse.fill` were the functions. The submodules stay
reachable via `sys.modules` / `importlib.import_module`, which is exactly how the
`engine.fieldmap` / `engine.providers.base` / `engine.discover` re-export shims
reach their moved members (never via the package attribute).

Kept LIGHT (matching the old module's import cost): importing this package loads
NO patchright / `engine.browse`; the fill/capture bodies still reach
`engine.fill`'s private helpers via CALL-TIME imports. This package does NOT
self-register with any registry yet (Stage 2e wires the new registry).
"""

# Eager-load the sibling submodules so the whole vendor namespace is populated
# when the package is imported (matches the old all-in-one module). Aliased to
# underscore names so they do NOT shadow the Provider callables re-exported
# below; the submodules remain reachable by their canonical dotted paths.
from engine.providers.greenhouse import (  # noqa: F401
    capture as _capture_mod,
    resolve as _resolve_mod,
)
from engine.providers.greenhouse.fill import (  # noqa: F401
    apply_url,
    capture,
    fill,
    resolve_values,
    vendor,
)

__all__ = ["vendor", "capture", "apply_url", "resolve_values", "fill"]
