"""Per-automation config loader.

An instance's config.yaml is the ONLY thing that distinguishes jobhunt from phd
from papers (plan 7.1: one engine, three thin instances). Everything the engine
needs to parameterise a run lives here: topic, id prefix, threshold, buffer size,
scoring axis weights, ATS pre-check rules, and channel classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

WEIGHT_SUM_TOLERANCE = 1e-6


class ConfigError(ValueError):
    """Raised when an instance config is missing a field or is inconsistent."""


@dataclass(frozen=True)
class Config:
    name: str
    topic: str
    id_prefix: str
    threshold: int
    buffer_size: int
    terminal_state: str
    ssot: str
    axes: dict[str, float]
    ats_rules: list[dict] = field(default_factory=list)
    automatable_vendors: tuple[str, ...] = ()

    def channel_for(self, vendor: str) -> str:
        """automatable when the destination ATS is public + login-free (7.7)."""
        return "automatable" if vendor in self.automatable_vendors else "manual"


def load_config(path: str | Path) -> Config:
    """Load + validate an instance config.yaml. Fails fast on bad input."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    _require(raw, ("name", "topic", "id_prefix", "threshold", "buffer_size",
                   "terminal_state", "ssot", "scoring"))
    axes = _validated_axes(raw["scoring"])
    channels = raw.get("channels", {})
    return Config(
        name=raw["name"],
        topic=raw["topic"],
        id_prefix=raw["id_prefix"],
        threshold=int(raw["threshold"]),
        buffer_size=int(raw["buffer_size"]),
        terminal_state=raw["terminal_state"],
        ssot=raw["ssot"],
        axes=axes,
        ats_rules=list(raw.get("ats_rules", [])),
        automatable_vendors=tuple(channels.get("automatable", [])),
    )


def _require(raw: dict, keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ConfigError(f"config missing required keys: {missing}")


def _validated_axes(scoring: dict) -> dict[str, float]:
    axes = scoring.get("axes")
    if not axes:
        raise ConfigError("scoring.axes must be a non-empty mapping")
    total = sum(float(w) for w in axes.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ConfigError(f"scoring.axes weights must sum to 1.0, got {total}")
    return {name: float(w) for name, w in axes.items()}
