"""The Claude models deskhand can run, their prices, and what a run has cost so far.

The CLI takes a short alias (opus, sonnet, fable); everything else works with a
ModelSpec. Prices are US dollars per million tokens from Anthropic's price list
as of 2026-10-05 and drive the per-run cost cap, so update them here when they
change. Cache writes use the 5-minute rate (1.25x input), which is what
automatic prompt caching writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ModelAlias(StrEnum):
    opus = "opus"
    sonnet = "sonnet"
    fable = "fable"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    input: float
    output: float
    cache_write: float
    cache_read: float


MODELS: dict[ModelAlias, ModelSpec] = {
    ModelAlias.opus: ModelSpec("claude-opus-5-5", 4.00, 20.00, 5.00, 0.20),
    ModelAlias.sonnet: ModelSpec("claude-sonnet-5-5", 2.00, 10.00, 2.50, 0.20),
    ModelAlias.fable: ModelSpec("claude-fable-5-1", 10.00, 50.00, 12.50, 0.25),
}
DEFAULT_MODEL = ModelAlias.opus


@dataclass
class Usage:
    """Running token totals and cost for one run.

    A refusal fallback can serve a turn from another model; that turn is still
    priced at the requested model's rates, which is close enough for a cap.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: Any, spec: ModelSpec) -> float:
        """Add one response's `usage` and return that turn's cost."""
        fresh = usage.input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        read = usage.cache_read_input_tokens or 0
        out = usage.output_tokens or 0
        cost = (
            fresh * spec.input
            + written * spec.cache_write
            + read * spec.cache_read
            + out * spec.output
        ) / 1_000_000
        self.input_tokens += fresh
        self.cache_write_tokens += written
        self.cache_read_tokens += read
        self.output_tokens += out
        self.cost_usd += cost
        return cost
