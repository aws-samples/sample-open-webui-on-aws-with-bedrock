# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rate resolution — one resolver shared by settle and estimate paths.

Precedence is exactly: operator override → AWS-published → unpriced
(Requirement 9.3). Rates are stored per 1,000,000 tokens (design D5);
`RateResult.per_token()` derives the per-token value at computation time.

Selection within the published grid (design §5):

  1. Override first. Overrides are flat per direction (design D3) — tier,
     routing and context are not consulted.
  2. Routing key: `geo` maps to `in_region` because AWS publishes no
     on-demand geo token rate (Requirement 7.4) — a literal `geo` key is
     preferred if one is ever published (Requirement 7.5). This mapping is
     NOT a fallback: it is the documented correct rate for geo routing, and
     stays auditable because `matched_routing` records the key that priced
     the request (Requirement 7.9's substitution marker is reserved for
     ladder exhaustion, where the request shape had no published rate).
  3. In-routing ladder, first hit wins:
     (tier, context) → (tier, default) → (standard, context) → (standard, default)
  4. Cross-routing fallback: repeat the ladder under the other published
     routing keys with `routing_fallback=True` (Requirements 7.7, 7.8).
  5. Otherwise unpriced: record tokens, price zero, never guess.

Row-shape tolerance: this resolver reads both the current row contract
(`rates` grid map + `_UNIT="USD/1M-tokens"`) and the legacy shapes that
precede the first refresh after an upgrade (flat per-token `input`/`output`
attributes, and the old `tiers` JSON attribute on published rows), so the
deploy-to-first-refresh window and preserved operator overrides
(Requirement 10.2) keep pricing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

UNIT_PER_1M = "USD/1M-tokens"

_TIER_STANDARD = "standard"
_CONTEXT_DEFAULT = "default"
_ROUTING_ORDER = ("in_region", "global", "geo")


@dataclass(frozen=True)
class RateResult:
    """Outcome of one (model, direction, tier, routing, context) resolution."""

    usd_per_1m: float | None
    source: str  # "override" | "aws-published" | "unpriced"
    matched_routing: str | None = None
    matched_tier: str | None = None
    matched_context: str | None = None
    matched_direction: str | None = None
    routing_fallback: bool = False   # priced under a routing the request did not use
    tier_fallback: bool = False      # tier or context substituted within the routing
    direction_fallback: bool = False # cache direction priced from another direction
    version: str = ""                # offer_version / override stamp of the supplying row

    @property
    def fallback(self) -> bool:
        """True when any substitution beyond the geo→in_region mapping occurred."""
        return self.routing_fallback or self.tier_fallback or self.direction_fallback

    def per_token(self) -> float | None:
        return None if self.usd_per_1m is None else self.usd_per_1m / 1_000_000.0


UNPRICED = RateResult(usd_per_1m=None, source="unpriced")


def unwrap_item(item) -> dict | list | float | str | bool | None:
    """Recursively convert a DynamoDB-typed item/value to plain Python.

    Numbers become floats (computation precision; the stored decimal string
    remains the exact record). Unknown type tags are passed through.
    """
    if isinstance(item, dict):
        if len(item) == 1:
            tag, val = next(iter(item.items()))
            if tag == "S":
                return val
            if tag == "N":
                return float(val)
            if tag == "BOOL":
                return bool(val)
            if tag == "NULL":
                return None
            if tag == "M":
                return {k: unwrap_item(v) for k, v in val.items()}
            if tag == "L":
                return [unwrap_item(v) for v in val]
            if tag == "SS":
                return list(val)
        return {k: unwrap_item(v) for k, v in item.items()}
    return item


def _as_float(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _override_rates(row: dict | None) -> dict:
    """Per-direction per-1M rates from an OVERRIDE row.

    Current shape: {"rates": {dir: per_1m}, "_UNIT": "USD/1M-tokens"}.
    Legacy shape (pre-migration, preserved per Requirement 10.2): flat
    per-token `input`/`output` attributes — normalized to per-1M here.
    """
    if not row:
        return {}
    rates = row.get("rates")
    if isinstance(rates, str):
        try:
            rates = json.loads(rates)
        except ValueError:
            rates = None
    if isinstance(rates, dict) and row.get("_UNIT") == UNIT_PER_1M:
        return {k: f for k, v in rates.items() if (f := _as_float(v)) is not None}
    out = {}
    for direction in ("input", "output", "cache_read", "cache_write"):
        v = _as_float(row.get(direction) or row.get(direction.replace("_", "-")))
        if v is not None:
            out[direction] = v * 1_000_000.0  # legacy rows store per-token
    return out


def _published_grid(row: dict | None) -> dict:
    """The routing → tier → context → direction grid from a PUBLISHED row.

    Legacy published rows (old refresher) carried a `tiers` JSON attribute of
    per-token rates with no routing/context axes; they are lifted into
    {in_region: {tier: {default: {dir: per_1m}}}} so the upgrade window
    between deploy and first refresh still prices (then GC replaces them).
    """
    if not row:
        return {}
    rates = row.get("rates")
    if isinstance(rates, str):
        try:
            rates = json.loads(rates)
        except ValueError:
            rates = None
    if isinstance(rates, dict) and row.get("_UNIT") == UNIT_PER_1M:
        return rates
    tiers = row.get("tiers")
    if isinstance(tiers, str):
        try:
            tiers = json.loads(tiers)
        except ValueError:
            tiers = None
    if isinstance(tiers, dict):
        lifted: dict = {}
        for tier, dirs in tiers.items():
            if not isinstance(dirs, dict):
                continue
            cell = {}
            for d, v in dirs.items():
                f = _as_float(v)
                if f is not None:
                    cell[d.replace("-", "_")] = f * 1_000_000.0
            if cell:
                lifted.setdefault(tier, {})[_CONTEXT_DEFAULT] = cell
        return {"in_region": lifted} if lifted else {}
    return {}


def _direction_chain(direction: str) -> list[str]:
    """Directions to try, most specific first (Requirements 6.4, 6.5).

    Cache-write durations fall back to the undated cache-write rate; any
    cache direction falls back to `input` last — every step beyond the
    first is a flagged substitution.
    """
    chain = [direction]
    if direction.startswith("cache_write_"):
        chain.append("cache_write")
    if direction.startswith("cache_") and direction != "input":
        chain.append("input")
    return chain


def _ladder(tier: str, context: str) -> list[tuple[str, str]]:
    steps = [
        (tier, context),
        (tier, _CONTEXT_DEFAULT),
        (_TIER_STANDARD, context),
        (_TIER_STANDARD, _CONTEXT_DEFAULT),
    ]
    seen, out = set(), []
    for step in steps:
        if step not in seen:
            seen.add(step)
            out.append(step)
    return out


def _search_routing(routing_grid: dict, direction: str, tier: str, context: str):
    """First ladder hit within one routing key: (value, tier, context, direction)."""
    for t, c in _ladder(tier, context):
        cell = routing_grid.get(t)
        cell = cell.get(c) if isinstance(cell, dict) else None
        if not isinstance(cell, dict):
            continue
        for d in _direction_chain(direction):
            v = _as_float(cell.get(d))
            if v is not None:
                return v, t, c, d
    return None


def resolve_rate(
    entry: dict,
    direction: str,
    tier: str = _TIER_STANDARD,
    routing: str = "in_region",
    context: str = _CONTEXT_DEFAULT,
) -> RateResult:
    """Resolve one rate from a catalog entry.

    `entry` is {"override": plain row | None, "published": plain row | None}
    (see `unwrap_item` for converting DynamoDB items). Returns UNPRICED-like
    result rather than raising; never invents a rate.
    """
    tier = tier or _TIER_STANDARD
    context = context or _CONTEXT_DEFAULT
    ov = _override_rates(entry.get("override"))
    if direction in ov:
        row = entry.get("override") or {}
        stamp = row.get("updated_at")
        return RateResult(
            usd_per_1m=ov[direction],
            source="override",
            matched_routing=routing,
            matched_tier=tier,
            matched_context=context,
            matched_direction=direction,
            version=f"override:{int(stamp)}" if isinstance(stamp, (int, float)) else "override",
        )

    grid = _published_grid(entry.get("published"))
    if grid:
        pub = entry.get("published") or {}
        version = str(pub.get("offer_version") or pub.get("price_map_version") or "")
        # geo maps to in_region unless a real geo key is published (Req 7.4/7.5)
        mapped = routing
        if routing == "geo" and "geo" not in grid:
            mapped = "in_region"
        candidates = [mapped] + [r for r in _ROUTING_ORDER if r != mapped]
        for i, rkey in enumerate(candidates):
            routing_grid = grid.get(rkey)
            if not isinstance(routing_grid, dict):
                continue
            hit = _search_routing(routing_grid, direction, tier, context)
            if hit:
                v, t, c, d = hit
                return RateResult(
                    usd_per_1m=v,
                    source="aws-published",
                    matched_routing=rkey,
                    matched_tier=t,
                    matched_context=c,
                    matched_direction=d,
                    routing_fallback=i > 0,
                    tier_fallback=t != tier or c != context,
                    direction_fallback=d != direction,
                    version=version,
                )
    return UNPRICED
