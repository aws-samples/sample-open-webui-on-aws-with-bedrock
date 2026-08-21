# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rate resolution — one resolver shared by settle and estimate paths.

Precedence is exactly: operator override -> AWS-published -> unpriced
(Requirement 9.3). Rates are stored per 1,000,000 tokens; ``RateResult`` /
``per_token()`` derives the per-token value at computation time.

Design 06-GATEWAY-PRICING-COVERAGE D6: the resolver reads BOTH override and
published rows through ONE ``_grid`` normalizer, and the routing x ladder x
direction fallback is a single ordered candidate-key CHAIN (an explicit,
readable, testable sequence) rather than three nested loops guarded by three
boolean flags. The legacy ``tiers``->grid lift and the flat per-token
``input``/``output`` tolerance are DELETED: verified against the live table,
zero published rows use either shape, and the current refresher never writes
them (design D6 "Live row shapes").

Selection within the published grid (design §5):

  1. Override first (flat per direction — tier/routing/context not consulted).
  2. Routing key: ``geo`` maps to ``in_region`` because AWS publishes no
     on-demand geo token rate (Requirement 7.4); a literal ``geo`` key wins
     if ever published (Requirement 7.5). The mapping is the documented
     correct rate, not a fallback — ``matched_routing`` records the priced key.
  3. In-routing ladder, first hit wins:
     (tier, context) -> (tier, default) -> (standard, context) -> (standard, default)
  4. Cross-routing fallback: repeat the ladder under the other routing keys
     with ``routing_fallback=True`` (Requirements 7.7, 7.8).
  5. Otherwise unpriced: record tokens, price zero, never guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator

UNIT_PER_1M = "USD/1M-tokens"

# ── D10: one cache TTL for the package (estimate + settle share it) ──────────
PRICING_CACHE_TTL_S = 300

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
        """True when any substitution beyond the geo->in_region mapping occurred."""
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


def _grid(row: dict | None) -> dict:
    """The ``rates`` grid map from a row, or {} — the ONE row normalizer (D6).

    Both OVERRIDE and PUBLISHED rows carry the same current contract:
    ``{"rates": <grid map>, "_UNIT": "USD/1M-tokens"}``. ``rates`` may arrive
    as a JSON string (some write paths persist it that way). Any other shape
    yields {} — the dead ``tiers`` lift and flat ``input``/``output``
    tolerance were removed (no live row uses them).
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
    return {}


def _override_rates(row: dict | None) -> dict:
    """Per-direction per-1M rates from an OVERRIDE row.

    Overrides are flat per direction: the override grid, if present, is a
    ``{direction: per_1m}`` map (design D3). Read through the shared ``_grid``
    normalizer, then flatten only the leaf direction map.
    """
    rates = _grid(row)
    return {k: f for k, v in rates.items() if (f := _as_float(v)) is not None}


def _direction_chain(direction: str) -> list[str]:
    """Directions to try, most specific first (Requirements 6.4, 6.5).

    A cache-write duration falls back to the undated cache-write rate; any
    cache direction falls back to ``input`` last — every step beyond the
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
    seen: set = set()
    out = []
    for step in steps:
        if step not in seen:
            seen.add(step)
            out.append(step)
    return out


@dataclass(frozen=True)
class _Candidate:
    routing: str
    tier: str
    context: str
    direction: str
    routing_fallback: bool
    tier_fallback: bool
    direction_fallback: bool


def _candidate_chain(direction: str, tier: str, routing: str, context: str,
                     grid: dict) -> Iterator[_Candidate]:
    """Yield resolution candidates in exact effective order (D6).

    One readable generator replaces the former 3-nested-loop + 3-boolean
    fallback matrix. Order (unchanged, proven by the golden test):
      routing: mapped-request-routing first, then the remaining published
               routings (geo->in_region mapping applied, not counted as a
               routing fallback);
      within a routing: the (tier, context) ladder;
      within a cell: the direction chain (most specific first).
    ``*_fallback`` flags are computed per candidate from how far it strayed
    from the requested (routing, tier, context, direction).
    """
    mapped = routing
    if routing == "geo" and "geo" not in grid:
        mapped = "in_region"
    routings = [mapped] + [r for r in _ROUTING_ORDER if r != mapped]
    for ri, rkey in enumerate(routings):
        for t, c in _ladder(tier, context):
            for d in _direction_chain(direction):
                yield _Candidate(
                    routing=rkey,
                    tier=t,
                    context=c,
                    direction=d,
                    routing_fallback=ri > 0,
                    tier_fallback=(t != tier or c != context),
                    direction_fallback=(d != direction),
                )


def _lookup(grid: dict, cand: _Candidate) -> float | None:
    cell = grid.get(cand.routing)
    cell = cell.get(cand.tier) if isinstance(cell, dict) else None
    cell = cell.get(cand.context) if isinstance(cell, dict) else None
    if not isinstance(cell, dict):
        return None
    return _as_float(cell.get(cand.direction))


def resolve_rate(
    entry: dict,
    direction: str,
    tier: str = _TIER_STANDARD,
    routing: str = "in_region",
    context: str = _CONTEXT_DEFAULT,
) -> RateResult:
    """Resolve one rate from a catalog entry.

    ``entry`` is {"override": plain row | None, "published": plain row | None}
    (see ``unwrap_item`` for converting DynamoDB items). Returns an
    UNPRICED-like result rather than raising; never invents a rate.
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

    grid = _grid(entry.get("published"))
    if grid:
        pub = entry.get("published") or {}
        version = str(pub.get("offer_version") or pub.get("price_map_version") or "")
        for cand in _candidate_chain(direction, tier, routing, context, grid):
            v = _lookup(grid, cand)
            if v is not None:
                return RateResult(
                    usd_per_1m=v,
                    source="aws-published",
                    matched_routing=cand.routing,
                    matched_tier=cand.tier,
                    matched_context=cand.context,
                    matched_direction=cand.direction,
                    routing_fallback=cand.routing_fallback,
                    tier_fallback=cand.tier_fallback,
                    direction_fallback=cand.direction_fallback,
                    version=version,
                )
    return UNPRICED
