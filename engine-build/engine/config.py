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
_ATTACH_MODES = ("per_item", "bundle", "none")


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
    # Full raw `scoring` block. `axes` (above) is the validated soft_fit weight
    # map extracted from it; `scoring` additionally carries the gated-multiplicative
    # knobs (family, seniority, skills, term_length, comp, eligibility, excludes,
    # commute) the Scorer reads. Kept as a plain dict so instances tune every
    # number in config.yaml with zero code change (W4 matching redesign).
    scoring: dict = field(default_factory=dict)
    ats_rules: list[dict] = field(default_factory=list)
    automatable_vendors: tuple[str, ...] = ()
    # W4 live-pipeline knobs. Defaults keep the phd/papers configs (which omit
    # these keys) loading unchanged; only jobhunt sets them today.
    draft_cap: int = 10
    drafter: dict = field(default_factory=lambda: {"model": "sonnet",
                                                   "effort": "medium"})
    sources: str | None = None
    # Attachment routing for drafted PDFs (W4 3.9): per_item ships one message
    # per drafted item, bundle ships a single zip, none keeps the digest only.
    # Default per_item so phd/papers configs (which omit the key) stay valid.
    attach_mode: str = "per_item"

    def channel_for(self, vendor: str) -> str:
        """automatable when the destination ATS is public + login-free (7.7)."""
        return "automatable" if vendor in self.automatable_vendors else "manual"


def load_config(path: str | Path) -> Config:
    """Load + validate an instance config.yaml. Fails fast on bad input."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    _require(raw, ("name", "topic", "id_prefix", "threshold", "buffer_size",
                   "terminal_state", "ssot", "scoring"))
    scoring = raw["scoring"]
    axes = _validated_axes(scoring)
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
        scoring=dict(scoring),
        ats_rules=list(raw.get("ats_rules", [])),
        automatable_vendors=tuple(channels.get("automatable", [])),
        draft_cap=int(raw.get("draft_cap", 10)),
        drafter=dict(raw.get("drafter") or {"model": "sonnet",
                                            "effort": "medium"}),
        sources=raw.get("sources"),
        attach_mode=_validated_attach_mode(raw.get("attach_mode", "per_item")),
    )


def _validated_attach_mode(value: str) -> str:
    if value not in _ATTACH_MODES:
        raise ConfigError(
            f"attach_mode must be one of {_ATTACH_MODES}, got {value!r}")
    return value


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
