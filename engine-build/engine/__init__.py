"""Shared automation engine core (W3, fixtures-only v1).

One engine, three thin instances (jobhunt / phd / papers). Instances carry only
config; this package holds all behaviour: SSOT loading, per-automation sqlite
store, discovery adapters, scoring, the queue state machine, the questionnaire
protocol, and the notify layer. No live network calls exist in v1.
"""

from engine.config import Config, load_config
from engine.ssot import MISSING, SSOT
from engine.store import Store
from engine.kernel.discover_base import Posting
from engine.match import ScoreBreakdown, Scorer, TokenOverlapSimilarity
from engine.queue_sm import QueueItem, QueueStateMachine, RerankResult
from engine.questionnaire import QItem, Questionnaire
from engine.notify import FakeTransport, load_credentials, render_digest

__all__ = [
    "Config",
    "load_config",
    "MISSING",
    "SSOT",
    "Store",
    "Posting",
    "run_discovery",
    "ScoreBreakdown",
    "Scorer",
    "TokenOverlapSimilarity",
    "QueueItem",
    "QueueStateMachine",
    "RerankResult",
    "QItem",
    "Questionnaire",
    "FakeTransport",
    "load_credentials",
    "render_digest",
]


def __getattr__(name: str):
    # PEP 562: keep `from engine import run_discovery` public without eagerly
    # importing the pipeline (engine.run pulls the whole run stack) at
    # `import engine` time. Resolved lazily on first attribute access.
    if name == "run_discovery":
        from engine.run import run_discovery

        return run_discovery
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
