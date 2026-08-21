# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Golden-rate harness (design 06-GATEWAY-PRICING-COVERAGE, validation plan).

Golden-FIRST refactor guard. This test drives the CURRENT resolver over the
real trimmed offer fixtures across the FULL grid the resolver supports —
every (identity x direction x tier x routing x context) cell — and compares
the effective rate outcomes against ``fixtures/golden_rates.json``.

Capture (once, before the D5-D7 refactor): set ``GOLDEN_CAPTURE=1`` and run
this test; it writes the golden file and passes. Thereafter it asserts the
refactor produced ZERO effective-rate deltas. Any intended delta for THIS
refactor must be zero; if AWS fixtures legitimately change later, re-capture
deliberately.

    GOLDEN_CAPTURE=1 python -m pytest metering/tests/test_golden_rates.py -q
    python -m pytest metering/tests/test_golden_rates.py -q   # assert-equal
"""

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ -> import pricing

from pricing import offers, resolver  # noqa: E402
from pricing.resolver import resolve_rate  # noqa: E402

GOLDEN = HERE / "fixtures" / "golden_rates.json"

_FILE_ORDER = [
    ("offer_foundation_models.json", "AmazonBedrockFoundationModels"),
    ("offer_bedrock.json", "AmazonBedrock"),
    ("offer_service.json", "AmazonBedrockService"),
]

# The full dimension grid the resolver understands. Kept explicit (not
# harvested from the fixtures) so the golden covers ladder/fallback cells
# that no fixture row populates directly — that is exactly where the
# candidate-chain refactor could silently drift.
_DIRECTIONS = ["input", "output", "cache_read", "cache_write",
               "cache_write_5m", "cache_write_1h", "cache_write_30m"]
_TIERS = ["standard", "batch", "flex", "priority", "latency_optimized"]
_ROUTINGS = ["in_region", "global", "geo"]
_CONTEXTS = ["default", "long"]


def _fixture(name: str) -> dict:
    return json.loads((HERE / "fixtures" / name).read_text(encoding="utf-8"))


def _build_grid(identity_wanted: str) -> dict:
    """Assemble routing->tier->context->direction grid for one identity,
    first-write-wins in refresher file order (mirrors the live catalog build)."""
    grid: dict = {}
    for fixture_name, svc in _FILE_ORDER:
        rates, _ = offers.parse_offer(_fixture(fixture_name), "us-east-1", svc)
        for r in rates:
            if r.identity != identity_wanted:
                continue
            cell = (grid.setdefault(r.routing, {})
                    .setdefault(r.tier, {}).setdefault(r.context, {}))
            cell.setdefault(r.direction, float(r.usd_per_1m))
    return grid


def _all_identities() -> list[str]:
    seen = []
    for fixture_name, svc in _FILE_ORDER:
        rates, _ = offers.parse_offer(_fixture(fixture_name), "us-east-1", svc)
        for r in rates:
            if r.identity not in seen:
                seen.append(r.identity)
    return sorted(seen)


def _entry(grid: dict) -> dict:
    return {"published": {"rates": grid, "_UNIT": resolver.UNIT_PER_1M,
                          "offer_version": "GOLDEN"}}


def _capture() -> dict:
    """Effective-rate outcomes for every identity x full-grid cell."""
    out: dict = {}
    for ident in _all_identities():
        grid = _build_grid(ident)
        if not grid:
            continue
        entry = _entry(grid)
        cells: dict = {}
        for direction in _DIRECTIONS:
            for tier in _TIERS:
                for routing in _ROUTINGS:
                    for context in _CONTEXTS:
                        rr = resolve_rate(entry, direction, tier=tier,
                                          routing=routing, context=context)
                        key = f"{direction}|{tier}|{routing}|{context}"
                        cells[key] = {
                            "usd_per_1m": rr.usd_per_1m,
                            "source": rr.source,
                            "matched_routing": rr.matched_routing,
                            "matched_tier": rr.matched_tier,
                            "matched_context": rr.matched_context,
                            "matched_direction": rr.matched_direction,
                            "routing_fallback": rr.routing_fallback,
                            "tier_fallback": rr.tier_fallback,
                            "direction_fallback": rr.direction_fallback,
                            "fallback": rr.fallback,
                        }
        out[ident] = cells
    return out


def test_golden_rates_stable():
    captured = _capture()
    if os.environ.get("GOLDEN_CAPTURE") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        return
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    deltas = []
    idents = sorted(set(captured) | set(expected))
    for ident in idents:
        exp_cells = expected.get(ident, {})
        got_cells = captured.get(ident, {})
        for cellkey in sorted(set(exp_cells) | set(got_cells)):
            e = exp_cells.get(cellkey)
            g = got_cells.get(cellkey)
            if e != g:
                deltas.append(f"{ident} [{cellkey}]: golden={e} now={g}")
    assert not deltas, "golden rate deltas (refactor must be zero):\n" + "\n".join(deltas)
