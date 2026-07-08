"""Provider registry package: the single source of truth for per-vendor ATS wiring.

W4 scattered the per-vendor branch across four call sites (fetch.endpoint_for +
_ADAPTERS + load_sources, fill._apply_url, run._collect_fieldmap). W5 consolidated
that knowledge into one `vendor -> ProviderSpec` map; W5.1 Stage 3c collapsed the
two coexisting registries onto the single eager-but-light auto-registry
`engine.providers._registry`, which the plugins populate by SELF-REGISTERING on
import. The call sites delegate to it; the capture / apply / endpoint function
BODIES live in their vendor plugin homes (`<vendor>.capture` / `.discover`,
plus `fill.greenhouse_apply_url`) and the registry only holds references.

POPULATION: importing this package eagerly imports the four vendor plugin
packages below, each of which self-registers into `_registry.PROVIDERS` from its
own `__init__`. That transit is what makes `_registry.PROVIDERS` fully populated
after any `engine.providers.*` import (it replaces the old `registry.py`, whose
discover-adapter imports used to trigger the same self-registration). Vendor
`__init__`s import `engine.fill` dataclasses (lever/ashby/workable `.fill`), so
the re-entrant load graph is the same one the old registry drove. The
`PROVIDERS` order is PINNED to `VENDOR_ORDER` below (an iteration order only --
no functional dependency; host matchers are disjoint): registration walks that
tuple explicitly, and a post-import check raises if a future import-graph edit
ever makes the self-registration cascade diverge from it.

`Provider` (protocol.py) is the structural CONTRACT the per-vendor packages
implement; re-exported here for a single import site
(`from engine.providers import Provider, resolve`).
"""

import importlib

from engine.providers.protocol import Provider
from engine.providers._registry import PROVIDERS, ProviderSpec, detect, resolve

# The canonical vendor order. Registration walks THIS tuple (not an emergent
# import cascade), so `PROVIDERS` iteration order is pinned by construction.
VENDOR_ORDER: tuple[str, ...] = ("greenhouse", "ashby", "lever", "workable")

# Import the four vendor plugin packages so they self-register into
# `_registry.PROVIDERS`. Kept AFTER the `_registry` re-export so `register` is
# defined before any vendor `__init__` calls it (the re-entrant edge).
for _vendor in VENDOR_ORDER:
    importlib.import_module(f"engine.providers.{_vendor}")

# Structural pin: a future import-graph edit that lets one plugin's load chain
# trigger another's registration mid-walk would silently reorder PROVIDERS;
# fail LOUDLY at import instead.
if tuple(PROVIDERS) != VENDOR_ORDER:
    raise RuntimeError(
        f"provider registration order {tuple(PROVIDERS)!r} diverged from the "
        f"pinned VENDOR_ORDER {VENDOR_ORDER!r}"
    )

__all__ = ["PROVIDERS", "Provider", "ProviderSpec", "VENDOR_ORDER", "detect",
           "resolve"]
