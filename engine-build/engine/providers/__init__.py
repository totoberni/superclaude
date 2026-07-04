"""Provider registry package: the single source of truth for per-vendor ATS wiring.

W4 scattered the per-vendor branch across four call sites (fetch.endpoint_for +
_ADAPTERS + load_sources, fill._apply_url, run._collect_fieldmap). W5 consolidates
that knowledge into one `vendor -> ProviderSpec` map here; the call sites delegate
to it. This is a behaviour-preserving CONSOLIDATION: the capture / apply / endpoint
function BODIES stay in their home modules (fieldmap.py, browse.py, fill.py) and the
registry only holds references to them.

`Provider` (protocol.py) is the structural CONTRACT the per-vendor modules
(greenhouse.py first, W5.2) implement on top of this registry; re-exported here
for a single import site (`from engine.providers import Provider, resolve`).
"""

from engine.providers.protocol import Provider
from engine.providers.registry import PROVIDERS, ProviderSpec, detect, resolve

__all__ = ["PROVIDERS", "Provider", "ProviderSpec", "detect", "resolve"]
