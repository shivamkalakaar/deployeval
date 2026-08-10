"""Published per-model token prices (USD per 1M tokens) for cost metering.

All values are PUBLIC list prices. Update as vendors change pricing; keep this file the single
source of truth so the cost meter never hard-codes rates elsewhere.

Cache multipliers follow the documented Bedrock/Anthropic convention:
  - cache WRITE (5-min TTL): 1.25x the input rate; (1-hour TTL): 2.0x the input rate
  - cache READ: 0.10x the input rate

If a model is missing here, the meter reports its token COUNTS but marks cost as unknown rather
than guessing (honesty rule: never fabricate a number).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelPrice:
    """Prices in USD per 1,000,000 tokens."""
    input_per_m: float
    output_per_m: float
    # cache write/read multipliers applied to input_per_m
    cache_write_5m_mult: float = 1.25
    cache_write_1h_mult: float = 2.0
    cache_read_mult: float = 0.10


# Keys are canonical short model names used across DeployEval (see models config).
# Values are PUBLIC list prices; verify against the vendor page before publishing a snapshot.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(input_per_m=5.0, output_per_m=25.0),
    "claude-sonnet-5": ModelPrice(input_per_m=3.0, output_per_m=15.0),
    "claude-haiku-4-5": ModelPrice(input_per_m=1.0, output_per_m=5.0),
    # Fable-5 list price to be confirmed before publishing; left out deliberately so cost is
    # reported as "unknown" rather than guessed if not filled in.
}


def price_for(model: str) -> Optional[ModelPrice]:
    """Return the ModelPrice for a canonical model name, or None if unknown."""
    return PRICES.get(model)
