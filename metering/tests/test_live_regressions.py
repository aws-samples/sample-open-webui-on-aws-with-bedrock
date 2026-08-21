# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression guards for the four defects the 2026-08-21 LIVE validation caught.

Fixtures could not show these; the deployed environment did:
1. nova-sonic $0.06→$3.40 — speech-token SKUs collided onto the text leaf
   (D7 keep-MAX then promoted the speech rate). Fix: non-text-modality
   exclusion by direction-anchor adjacency; text-* is qualifier noise.
2. 318 "unclassified" dimensions — non-token products (images, minutes, TPM)
   polluted the alarmed bucket. Fix: only token-unit SKUs are in scope.
3. Coverage reported operator-overridden models as unpriced (the join never
   read OVERRIDE rows). Fix: settle-shaped resolve with the override row.
4. GET /pricing/coverage 404'd after the first refresh (the miss was cached).
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from pricing import offers  # noqa: E402
from pricing.offers import EXCLUDED, GRID, UNCLASSIFIED, classify_usagetype  # noqa: E402
from test_pricing_refresher import (  # noqa: E402
    CP_MODELS, FIXTURE_BY_SERVICE, FakeDdb, _fixture, _load_refresher,
)


# ── 1. non-text modality exclusion (nova-sonic live regression) ─────────────

def test_speech_tokens_are_excluded_not_text_leaf():
    for ut in ("USE1-NovaSonic-speech-input-tokens",
               "USE1-NovaSonic-speech-output-tokens",
               "USE1-NovaSonic2.0-speech-input-tokens"):
        v = classify_usagetype(ut, "AmazonBedrock")
        assert v.kind == EXCLUDED and v.rule == "non-text-modality", ut


def test_modality_after_direction_is_excluded_too():
    # the Nova Omni grammar puts the modality AFTER the direction
    for ut in ("USE1-Nova2.0Omni-input-audio-token-count",
               "USE1-Nova2.0Omni-input-image-token-count-batch",
               "USE1-MP:USE1_InputImageCount-Units",
               "USE1-MP:USE1_inputVideoSecond-Units"):
        v = classify_usagetype(ut, "svc")
        assert v.kind == EXCLUDED and v.rule == "non-text-modality", ut


def test_text_tokens_classify_to_the_plain_leaf():
    v = classify_usagetype("USE1-NovaSonic-text-input-tokens", "AmazonBedrock")
    assert v.kind == GRID and v.classified.direction == "input"
    v = classify_usagetype("USE1-NovaSonic-text-output-tokens", "AmazonBedrock")
    assert v.kind == GRID and v.classified.direction == "output"


def test_modality_word_inside_model_name_does_not_poison_text_skus():
    # adjacency, not substring: a model NAMED "...video..." keeps its text rates
    v = classify_usagetype("USE1-acme.video-genie-v1-mantle-input-tokens", "svc")
    assert v.kind == GRID and v.classified.direction == "input"


def test_nova_sonic_fixture_prices_text_rates():
    """End-to-end on the real fixture rows: input $0.06/1M, output $0.24/1M —
    the speech rates ($3.40/$13.60) must not win the leaf."""
    rates, _ = offers.parse_offer(
        _fixture("offer_bedrock.json"), "us-east-1", "AmazonBedrock")
    sonic = [r for r in rates if r.identity == "Nova Sonic"]
    assert sonic, "Nova Sonic text SKUs must parse"
    grid: dict = {}
    acc = offers.ParseAccounting()
    for r in sonic:
        offers.merge_rate(grid, r, acc, model_id="Nova Sonic")
    leaf = grid["in_region"]["standard"]["default"]
    assert float(leaf["input"]) == 0.06
    assert float(leaf["output"]) == 0.24
    assert not acc.rate_conflicts, "speech exclusion must remove the leaf conflict"


# ── 2. unclassified scope = token-unit SKUs only ─────────────────────────────


def test_leading_qualifiers_are_consumed_not_discarded():
    """The camel MP family spells qualifiers BEFORE the direction anchor
    (MillionBatchOutputTokens). Discarding all leading tokens as name
    fragments silently reclassified batch rates onto the standard leaf —
    the residual rate_conflicts the round-2 live validation found."""
    v = classify_usagetype("USE1-MP:USE1_MillionBatchOutputTokens-Units", "svc")
    assert v.kind == GRID and (v.classified.direction, v.classified.tier) == ("output", "batch")
    v = classify_usagetype("USE1-MP:USE1_MillionBatchInputTokens-Units", "svc")
    assert v.kind == GRID and (v.classified.direction, v.classified.tier) == ("input", "batch")
    # true name fragments before the anchor are still discarded
    v = classify_usagetype("USE1-Claude3Haiku-input-tokens", "svc")
    assert v.kind == GRID and (v.classified.direction, v.classified.tier) == ("input", "standard")

def test_non_token_unknown_shapes_stay_out_of_the_unclassified_bucket():
    offer = {
        "version": "t", "products": {
            "SKU1": {"attributes": {"usagetype": "USE1-DataAutomation-Custom-ImagesProcessed"}},
            "SKU2": {"attributes": {"usagetype": "USE1-Totally-Unknown-Token-Shape-input-tokens-wat",
                                    "model": "mystery"}},
        },
        "terms": {"OnDemand": {
            "SKU1": {"t": {"priceDimensions": {"d": {"unit": "Images Processed",
                                                     "pricePerUnit": {"USD": "0.005"}}}}},
            "SKU2": {"t": {"priceDimensions": {"d": {"unit": "1K tokens",
                                                     "pricePerUnit": {"USD": "0.001"}}}}},
        }},
    }
    acc = offers.ParseAccounting()
    offers.parse_offer(offer, "us-east-1", "AmazonBedrock", acc)
    uts = [u["usage_type"] for u in acc.unclassified]
    assert "USE1-DataAutomation-Custom-ImagesProcessed" not in uts, \
        "non-token product must not pollute the alarmed bucket"
    assert "USE1-Totally-Unknown-Token-Shape-input-tokens-wat" in uts, \
        "a TOKEN-unit unknown must still be loud"


# ── 3. coverage honors operator OVERRIDE rows ────────────────────────────────

def test_coverage_counts_override_priced_models_as_priced():
    mod = _load_refresher()
    fake = FakeDdb()
    gap = "openai.gpt-5.6-luna"
    # operator override row exactly as the admin API writes it (per-1M grid)
    fake.seed({
        "pk": {"S": f"PRICING#{gap}"}, "sk": {"S": "OVERRIDE"},
        "rates": {"M": {"input": {"N": "0.22"}, "output": {"N": "1.32"}}},
        "_UNIT": {"S": "USD/1M-tokens"},
        "note": {"S": "AWS model-card page, fetched 2026-08-21"},
    })
    metrics: list = []
    mod.ddb = fake
    mod._metric = lambda name, value=1, unit="Count": metrics.append((name, value))
    mod._list_cp_models = lambda: CP_MODELS
    mod._mantle_catalog = lambda: [gap, "zai.glm-4.6"]
    mod._served_caps = lambda: {"responses": [gap], "chat_completions": ["zai.glm-4.6"]}
    mod._fetch_offer = lambda svc: _fixture(FIXTURE_BY_SERVICE[svc])
    mod.handler({}, None)

    item = fake.items[("PRICING#_COVERAGE", "META")]
    models = {m["M"]["id"]["S"]: m["M"] for m in item["models"]["L"]}
    assert models[gap]["priced"]["BOOL"] is True
    assert models[gap]["source"]["S"] == "OVERRIDE"
    assert models["zai.glm-4.6"]["priced"]["BOOL"] is False
    assert models["zai.glm-4.6"]["reason"]["S"] == "no-pricing-row"
    assert int(item["counts"]["M"]["invokable_unpriced"]["N"]) == 1
    assert dict(metrics).get("UnpricedGatewayModels") == 1


# ── 4. coverage endpoint must not cache the 404 miss ─────────────────────────

def test_coverage_404_is_not_cached(monkeypatch):
    import importlib.util
    import os
    os.environ.setdefault("TABLE", "t")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    spec = importlib.util.spec_from_file_location(
        "admin_api_cov", HERE.parent / "admin-api" / "index.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules["admin_api_cov"] = api
    spec.loader.exec_module(api)

    calls = {"n": 0}
    state = {"item": None}

    def fake_read():
        calls["n"] += 1
        return state["item"]

    monkeypatch.setattr(api, "_read_coverage_item", fake_read)
    api._read_cache.clear()

    def hit_route():
        event = {"routeKey": "GET /pricing/coverage",
                 "requestContext": {"authorizer": {"jwt": {"claims": {
                     "sub": "t", "cognito:groups": "admin"}}}}}
        return api.handler(event, None)

    # first call: absent -> 404, and the miss must NOT be cached
    r1 = hit_route()
    assert r1["statusCode"] == 404
    # item appears (refresher ran); the very next call must see it
    state["item"] = {"counts": {"invokable_unpriced": 1}, "models": []}
    r2 = hit_route()
    assert r2["statusCode"] == 200, "the 404 miss was cached (live 2026-08-21 defect)"
    assert json.loads(r2["body"])["counts"]["invokable_unpriced"] == 1
    # and the SUCCESS is cached (read fn not called again within TTL)
    n = calls["n"]
    r3 = hit_route()
    assert r3["statusCode"] == 200 and calls["n"] == n
