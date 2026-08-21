# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Handler-level coverage-join wiring guard (adversarial review MAJOR-2).

test_coverage_join.py proves _build_coverage() is CORRECT; nothing there
proved handler() still CALLS it. This file runs the real handler end to end
(fixtures + fakes, network seams stubbed) and asserts the externally
observable effects of the join: the PRICING#_COVERAGE item is written with
the gpt-5.6 publishing-gap model counted invokable-unpriced, and the §3
gauges — including the PricingCoverageComputed staleness heartbeat
(MAJOR-1) — are emitted. If a refactor ever unwires the coverage join from
the refresh run, THIS test fails.
"""

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # metering/ -> import pricing
sys.path.insert(0, str(HERE))          # tests/    -> import test_pricing_refresher

from test_pricing_refresher import (  # noqa: E402
    CP_MODELS, FIXTURE_BY_SERVICE, FakeDdb, _fixture, _load_refresher,
)

# A publishing-gap model: served in caps + available in the catalog, but with
# NO SKU in any offer fixture (mirrors the live openai.gpt-5.6-* finding).
GAP_MODEL = "openai.gpt-5.6-luna"


def _run_with_gateway(mod, fake, catalog_ids, caps):
    """test_pricing_refresher._run, but with REAL gateway-seam stubs and a
    metric recorder instead of a no-op."""
    metrics: list = []
    mod.ddb = fake
    mod._metric = lambda name, value=1, unit="Count": metrics.append((name, value))
    mod._list_cp_models = lambda: CP_MODELS
    mod._mantle_catalog = lambda: list(catalog_ids)
    mod._served_caps = lambda: dict(caps)
    mod._fetch_offer = lambda svc: _fixture(FIXTURE_BY_SERVICE[svc])
    result = mod.handler({}, None)
    return result, metrics


def test_handler_runs_coverage_join_and_emits_gauges():
    mod = _load_refresher()
    fake = FakeDdb()
    priced_id = CP_MODELS[0][0]  # a control-plane model the fixtures price
    _, metrics = _run_with_gateway(
        mod, fake,
        catalog_ids=[GAP_MODEL, priced_id],
        caps={"responses": [GAP_MODEL], "chat_completions": [priced_id]},
    )

    # 1. The coverage item was written by the refresh run itself.
    item = fake.items.get(("PRICING#_COVERAGE", "META"))
    assert item is not None, "handler() no longer writes the coverage item"

    # 2. The publishing-gap model is a named, counted condition.
    models = {m["M"]["id"]["S"]: m["M"] for m in item["models"]["L"]}
    gap = models[GAP_MODEL]
    assert gap["priced"]["BOOL"] is False
    assert gap["reason"]["S"] == "no-pricing-row"
    assert gap["catalog_available"]["BOOL"] is True
    unpriced = int(item["counts"]["M"]["invokable_unpriced"]["N"])
    assert unpriced >= 1

    # 3. The §3 gauges came from THIS handler run — including the value that
    #    drives UnpricedGatewayModelsAlarm and the staleness heartbeat.
    emitted = dict(m for m in metrics)
    assert emitted.get("UnpricedGatewayModels") == unpriced
    assert emitted.get("PricingCoverageComputed") == 1
    assert "PricingUnmatchedActionable" in emitted
    assert "PricingDimensionUnclassified" in emitted


def test_handler_catalog_failure_still_writes_partial_coverage():
    """Catalog fetch failing must not erase the coverage surface: the item is
    written from the served caps alone, the error is recorded, and the
    heartbeat still fires (a broken gateway probe is not a stopped join)."""
    mod = _load_refresher()
    fake = FakeDdb()

    def boom():
        raise OSError("mantle catalog unreachable")

    metrics: list = []
    mod.ddb = fake
    mod._metric = lambda name, value=1, unit="Count": metrics.append((name, value))
    mod._list_cp_models = lambda: CP_MODELS
    mod._mantle_catalog = boom
    mod._served_caps = lambda: {"responses": [GAP_MODEL]}
    mod._fetch_offer = lambda svc: _fixture(FIXTURE_BY_SERVICE[svc])
    mod.handler({}, None)

    item = fake.items.get(("PRICING#_COVERAGE", "META"))
    assert item is not None
    assert "error" in item, "catalog failure must be recorded on the item"
    emitted = dict(m for m in metrics)
    assert emitted.get("PricingCoverageComputed") == 1
