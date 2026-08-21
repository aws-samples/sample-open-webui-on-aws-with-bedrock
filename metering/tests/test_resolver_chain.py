# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Resolver candidate-chain order (design D6) + shared cache TTL (design D10).

The former 3-nested-loop + 3-boolean-flag fallback matrix is now a single
ordered candidate-key generator. These tests assert the chain's ORDER as data
(routing outer, (tier, context) ladder, direction chain innermost) and the
per-candidate fallback-flag derivation — the properties the golden test
exercises end-to-end, pinned here directly.
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ -> import pricing

import pricing  # noqa: E402
from pricing import resolver  # noqa: E402
from pricing.resolver import PRICING_CACHE_TTL_S, resolve_rate  # noqa: E402


def _chain(direction, tier, routing, context, grid):
    return list(resolver._candidate_chain(direction, tier, routing, context, grid))


def test_candidate_chain_routing_is_outermost():
    grid = {"in_region": {}, "global": {}, "geo": {}}
    cands = _chain("input", "standard", "in_region", "default", grid)
    # requested routing first, then the remaining published order
    routings_in_order = []
    for c in cands:
        if c.routing not in routings_in_order:
            routings_in_order.append(c.routing)
    assert routings_in_order == ["in_region", "global", "geo"]
    # all in_region candidates precede the first global candidate (outermost)
    first_global = next(i for i, c in enumerate(cands) if c.routing == "global")
    assert all(c.routing == "in_region" for c in cands[:first_global])


def test_candidate_chain_ladder_then_direction_order():
    grid = {"in_region": {}}
    cands = [c for c in _chain("cache_write_5m", "flex", "in_region", "long", grid)
             if c.routing == "in_region"]
    # ladder order: (flex,long) (flex,default) (standard,long) (standard,default)
    ladder_seen = []
    for c in cands:
        key = (c.tier, c.context)
        if key not in ladder_seen:
            ladder_seen.append(key)
    assert ladder_seen == [("flex", "long"), ("flex", "default"),
                           ("standard", "long"), ("standard", "default")]
    # within the first ladder cell, direction chain is most-specific first
    first_cell = [c.direction for c in cands
                  if (c.tier, c.context) == ("flex", "long")]
    assert first_cell == ["cache_write_5m", "cache_write", "input"]


def test_candidate_flags_match_distance_from_request():
    grid = {"in_region": {}, "global": {}}
    cands = _chain("input", "flex", "in_region", "long", grid)
    exact = next(c for c in cands if (c.routing, c.tier, c.context, c.direction)
                 == ("in_region", "flex", "long", "input"))
    assert not (exact.routing_fallback or exact.tier_fallback or exact.direction_fallback)
    strayed = next(c for c in cands if (c.routing, c.tier, c.context)
                   == ("global", "standard", "default"))
    assert strayed.routing_fallback and strayed.tier_fallback


def test_geo_maps_to_in_region_and_is_not_a_routing_fallback():
    grid = {"in_region": {"standard": {"default": {"input": 5.5}}}}
    r = resolve_rate({"published": {"rates": grid, "_UNIT": resolver.UNIT_PER_1M}},
                     "input", routing="geo")
    assert r.usd_per_1m == 5.5 and r.matched_routing == "in_region"
    assert not r.routing_fallback and not r.fallback


def test_first_hit_along_the_chain_wins():
    # a lower-priority cell also carries a rate; the exact cell must win
    grid = {"in_region": {
        "flex": {"default": {"input": 9.9}},
        "standard": {"default": {"input": 5.5}},
    }}
    entry = {"published": {"rates": grid, "_UNIT": resolver.UNIT_PER_1M}}
    assert resolve_rate(entry, "input", tier="flex").usd_per_1m == 9.9
    r = resolve_rate(entry, "input", tier="priority")  # no priority cell
    assert r.usd_per_1m == 5.5 and r.matched_tier == "standard" and r.tier_fallback


# ── D10: one shared cache TTL exported from the package ─────────────────────

def test_pricing_cache_ttl_is_one_exported_constant():
    assert PRICING_CACHE_TTL_S == 300
    # exported at the package top level so both estimate and settle import it
    assert pricing.PRICING_CACHE_TTL_S is PRICING_CACHE_TTL_S
    assert resolver.PRICING_CACHE_TTL_S is PRICING_CACHE_TTL_S
