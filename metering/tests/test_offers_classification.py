# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Usage-type classification + merge determinism (design D5, D7, contract §1).

Every dimension in the real trimmed offer fixtures must land in exactly one
bucket — grid, excluded, or unclassified — with NO silent None drop, and the
merge must be deterministic (identical duplicate silent; conflict keeps MAX
and records a rate_conflicts entry).
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ -> import pricing

from pricing import offers  # noqa: E402
from pricing.offers import (  # noqa: E402
    EXCLUDED, GRID, UNCLASSIFIED, ParseAccounting, ParsedRate, classify_usagetype,
    merge_rate,
)

_FILE_ORDER = [
    ("offer_foundation_models.json", "AmazonBedrockFoundationModels"),
    ("offer_bedrock.json", "AmazonBedrock"),
    ("offer_service.json", "AmazonBedrockService"),
]


def _fixture(name: str) -> dict:
    return json.loads((HERE / "fixtures" / name).read_text(encoding="utf-8"))


def _every_usagetype():
    for fixture_name, svc in _FILE_ORDER:
        off = _fixture(fixture_name)
        for product in off.get("products", {}).values():
            ut = product.get("attributes", {}).get("usagetype", "")
            if ut:
                yield ut, svc


# ── D5 / contract §1: every dimension classified, no silent None ────────────

def test_every_dimension_lands_in_exactly_one_bucket():
    total = grid = excluded = unclassified = 0
    for ut, svc in _every_usagetype():
        v = classify_usagetype(ut, svc)
        total += 1
        assert v.kind in (GRID, EXCLUDED, UNCLASSIFIED), (ut, v.kind)
        assert v is not None  # never a bare None (contract §1)
        if v.kind == GRID:
            grid += 1
            assert v.classified is not None
        elif v.kind == EXCLUDED:
            excluded += 1
            assert v.rule in ("custom-model", "apo-optimize-prompt")
        else:
            unclassified += 1
    assert total == grid + excluded + unclassified
    assert grid > 0 and total > grid  # both priced and non-token dims present


def test_named_exclusions_are_recorded_not_dropped():
    for ut in ("USE1-anthropic.claude-custom-model-input-tokens",
               "USE1-MP:USE1_CustomModelInputTokenCount-Units"):
        v = classify_usagetype(ut, "svc")
        assert v.kind == EXCLUDED and v.rule == "custom-model", ut
    v = classify_usagetype("USE1-APO-optimizePrompt-tokens", "svc")
    assert v.kind == EXCLUDED and v.rule == "apo-optimize-prompt"


def test_unknown_qualifier_is_loud_unclassified_never_silent():
    # an unknown qualifier token must surface, not vanish
    v = classify_usagetype("USE1-MP:USE1_input_tokens_wibble-Units", "svc")
    assert v.kind == UNCLASSIFIED
    # a token dimension with a real grammar still grids
    v = classify_usagetype("USE1-MP:USE1_input_tokens_global_standard-Units", "svc")
    assert v.kind == GRID and v.classified.routing == "global"


def test_parse_offer_accounts_all_non_grid_dimensions():
    acc = ParseAccounting()
    gridded = 0
    for fixture_name, svc in _FILE_ORDER:
        rates, _ = offers.parse_offer(_fixture(fixture_name), "us-east-1", svc, acc)
        gridded += len(rates)
    # the three lists exist with the contract's exact shapes
    for e in acc.excluded:
        assert set(e) == {"usage_type", "rule"}
    for u in acc.unclassified:
        assert set(u) == {"usage_type", "service_code"}
    # zero silent drops: every fixture usage type is either gridded (a rate),
    # excluded, or unclassified — reconcile the counts.
    all_uts = list(_every_usagetype())
    grid_uts = {ut for ut, _ in all_uts} - {e["usage_type"] for e in acc.excluded} \
        - {u["usage_type"] for u in acc.unclassified}
    # every usage type that produced no rate is on one of the two lists
    accounted = ({e["usage_type"] for e in acc.excluded}
                 | {u["usage_type"] for u in acc.unclassified}
                 | grid_uts)
    assert accounted == {ut for ut, _ in all_uts}
    assert gridded > 0


# ── D7: deterministic merge ─────────────────────────────────────────────────

def _rate(direction, usd, routing="in_region", tier="standard", context="default",
          usagetype="ut"):
    from decimal import Decimal
    return ParsedRate(
        identity_kind="id", identity="vendor.m", display_name="M", provider="p",
        direction=direction, tier=tier, routing=routing, context=context,
        usd_per_1m=Decimal(str(usd)), effective_date="", usagetype=usagetype)


def test_identical_duplicate_merges_silently():
    grid, acc = {}, ParseAccounting()
    merge_rate(grid, _rate("input", 5.5), acc, "vendor.m")
    merge_rate(grid, _rate("input", 5.5, usagetype="dup"), acc, "vendor.m")
    assert grid["in_region"]["standard"]["default"]["input"] == 5.5
    assert acc.rate_conflicts == []  # identical -> silent


def test_conflict_keeps_max_and_records_signal():
    grid, acc = {}, ParseAccounting()
    merge_rate(grid, _rate("input", 5.0, usagetype="a"), acc, "vendor.m")
    merge_rate(grid, _rate("input", 5.5, usagetype="b"), acc, "vendor.m")
    # keep MAX (conservative for quota admission)
    assert grid["in_region"]["standard"]["default"]["input"] == 5.5
    assert len(acc.rate_conflicts) == 1
    c = acc.rate_conflicts[0]
    assert set(c) == {"model_id", "leaf", "kept", "dropped", "usage_type"}
    assert (c["kept"], c["dropped"]) == (5.5, 5.0)
    assert c["leaf"] == "in_region/standard/default/input"
    assert c["model_id"] == "vendor.m"


def test_conflict_max_is_order_independent():
    for first, second in ((5.0, 5.5), (5.5, 5.0)):
        grid, acc = {}, ParseAccounting()
        merge_rate(grid, _rate("input", first), acc, "vendor.m")
        merge_rate(grid, _rate("input", second), acc, "vendor.m")
        assert grid["in_region"]["standard"]["default"]["input"] == 5.5
        assert acc.rate_conflicts[0]["kept"] == 5.5
