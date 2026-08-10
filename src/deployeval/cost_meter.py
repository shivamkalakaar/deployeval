"""Token cost metering from a Claude Code session JSONL transcript.

Two load-bearing gotchas (verified on a live transcript by the harness spec):
  1. DEDUPE BY message.id. Streaming emits one assistant message as many JSONL rows sharing an id,
     each carrying the IDENTICAL final usage. Summing all rows ~doubles the cost. We keep one row
     per message.id.
  2. message.model is unreliable (stamped with session/parent model). Model identity for pricing
     comes from the run manifest, NOT the transcript. Token COUNTS are true regardless.

Honesty rule (DESIGN §7): if a transcript is truncated/incomplete, the cost is a FLOOR, never a
back-filled estimate. If the model's price is unknown, report counts with cost=None.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .prices import price_for


@dataclass
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    messages_counted: int = 0
    rows_seen: int = 0
    duplicate_rows_skipped: int = 0


def _iter_usage_rows(jsonl_path: Path):
    """Yield (message_id, usage_dict) for assistant messages that carry usage."""
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = row.get("message") or {}
            usage = msg.get("usage")
            mid = msg.get("id")
            if usage and mid:
                yield mid, usage


def sum_tokens(jsonl_path: Path) -> TokenTotals:
    """Sum token usage, deduped by message.id."""
    t = TokenTotals()
    seen: set[str] = set()
    for mid, usage in _iter_usage_rows(jsonl_path):
        t.rows_seen += 1
        if mid in seen:
            t.duplicate_rows_skipped += 1
            continue
        seen.add(mid)
        t.messages_counted += 1
        t.input_tokens += int(usage.get("input_tokens", 0) or 0)
        t.output_tokens += int(usage.get("output_tokens", 0) or 0)
        # cache fields may be split into 5m / 1h ephemeral buckets or a flat number
        cw = usage.get("cache_creation_input_tokens", 0) or 0
        cc = usage.get("cache_creation") or {}
        t.cache_write_5m += int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
        t.cache_write_1h += int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
        # if only the flat field is present, treat it as 5m-tier write
        if not cc and cw:
            t.cache_write_5m += int(cw)
        t.cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
    return t


def cost_usd(totals: TokenTotals, model: str) -> Optional[float]:
    """Compute USD cost for the given model; None if the model price is unknown."""
    p = price_for(model)
    if p is None:
        return None
    m = 1_000_000
    return round(
        totals.input_tokens / m * p.input_per_m
        + totals.output_tokens / m * p.output_per_m
        + totals.cache_write_5m / m * p.input_per_m * p.cache_write_5m_mult
        + totals.cache_write_1h / m * p.input_per_m * p.cache_write_1h_mult
        + totals.cache_read / m * p.input_per_m * p.cache_read_mult,
        4,
    )


def meter(jsonl_path: str | Path, model: str, truncated: bool = False) -> dict:
    """Full metering result for one trial. `model` is the canonical name from the run manifest."""
    p = Path(jsonl_path)
    totals = sum_tokens(p)
    usd = cost_usd(totals, model)
    return {
        "model": model,
        "transcript": str(p),
        "token_totals": asdict(totals),
        "token_cost_usd": usd,
        "cost_is_floor": truncated,       # honesty: floor if transcript was cut off
        "cost_known": usd is not None,    # False if model price missing (never guessed)
    }
