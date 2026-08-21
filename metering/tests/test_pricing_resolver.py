# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pricing identity + rate-resolution tests (pure functions, no AWS).

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q

Fixture rates are trimmed REAL offer-file excerpts (metering/tests/fixtures/),
so the dollar assertions here are the rates AWS actually publishes.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ → import pricing

from pricing import identity, offers, resolver  # noqa: E402
from pricing.identity import parse_model_ref  # noqa: E402
from pricing.resolver import resolve_rate  # noqa: E402


def _fixture(name: str) -> dict:
    return json.loads((HERE / "fixtures" / name).read_text(encoding="utf-8"))


def _grid(service_files: list[tuple[str, str]], identity_wanted: str) -> dict:
    """Assemble a routing→tier→context→direction grid for one Price List name/id
    from the real fixtures (first-write-wins, refresher file order)."""
    grid: dict = {}
    for fixture_name, svc in service_files:
        rates, _ = offers.parse_offer(_fixture(fixture_name), "us-east-1", svc)
        for r in rates:
            if r.identity != identity_wanted:
                continue
            cell = grid.setdefault(r.routing, {}).setdefault(r.tier, {}).setdefault(r.context, {})
            cell.setdefault(r.direction, float(r.usd_per_1m))
    return grid


_FILE_ORDER = [
    ("offer_foundation_models.json", "AmazonBedrockFoundationModels"),
    ("offer_bedrock.json", "AmazonBedrock"),
    ("offer_service.json", "AmazonBedrockService"),
]

OPUS5 = {"published": {"rates": _grid(_FILE_ORDER, "Claude Opus 5"),
                       "_UNIT": resolver.UNIT_PER_1M, "offer_version": "20260728133434"}}
SONNET4 = {"published": {"rates": _grid(_FILE_ORDER, "Claude Sonnet 4"),
                         "_UNIT": resolver.UNIT_PER_1M, "offer_version": "20260728133434"}}
HAIKU45 = {"published": {"rates": _grid(_FILE_ORDER, "Claude Haiku 4.5"),
                         "_UNIT": resolver.UNIT_PER_1M, "offer_version": "20260728133434"}}


# ── parse_model_ref: routing derivation (Req 7.1-7.4, 2.8, 2.9 / test 12.6, 12.7) ──

def test_bare_id_prices_in_region():
    ref = parse_model_ref("anthropic.claude-opus-5")
    assert ref == identity.ModelRef("anthropic.claude-opus-5", "in_region")


def test_global_prefix_routes_global():
    ref = parse_model_ref("global.anthropic.claude-opus-5")
    assert ref == identity.ModelRef("anthropic.claude-opus-5", "global")


def test_geo_prefixes_route_geo():
    for scope in ("us", "eu", "apac", "ap", "ca", "sa"):
        ref = parse_model_ref(f"{scope}.anthropic.claude-opus-5")
        assert ref == identity.ModelRef("anthropic.claude-opus-5", "geo"), scope


def test_gateway_and_pipe_prefixes_stripped():
    assert parse_model_ref("bedrock/anthropic.claude-opus-5").key == "anthropic.claude-opus-5"
    assert parse_model_ref("bedrock/global.anthropic.claude-opus-5") == identity.ModelRef(
        "anthropic.claude-opus-5", "global")
    assert parse_model_ref("gateway_anthropic.anthropic.claude-haiku-4-5").key == "anthropic.claude-haiku-4-5"
    assert parse_model_ref("metering.qwen.qwen3-32b").key == "qwen.qwen3-32b"


def test_scope_variants_share_one_catalog_key():
    keys = {parse_model_ref(m).key for m in (
        "anthropic.claude-opus-5",
        "us.anthropic.claude-opus-5",
        "global.anthropic.claude-opus-5",
        "bedrock/us.anthropic.claude-opus-5",
    )}
    assert keys == {"anthropic.claude-opus-5"}


def test_only_one_scope_is_peeled():
    # a second leading scope stays part of the key — never peel twice
    ref = parse_model_ref("us.eu.anthropic.claude-opus-5")
    assert ref.routing == "geo" and ref.key == "eu.anthropic.claude-opus-5"


def test_routing_default_applies_only_without_prefix(monkeypatch):
    monkeypatch.setenv("ROUTING_DEFAULT", "global")
    assert parse_model_ref("anthropic.claude-opus-5").routing == "global"
    # id-derived routing wins over the default (Req 7.11)
    assert parse_model_ref("us.anthropic.claude-opus-5").routing == "geo"
    monkeypatch.setenv("ROUTING_DEFAULT", "bogus")
    assert parse_model_ref("anthropic.claude-opus-5").routing == "in_region"


# ── alias expansion safety (Req 2.5, 2.6 / test 12.4) ──

def test_alias_expansion_strips_version_only_suffixes():
    assert identity.id_aliases("anthropic.claude-haiku-4-5-20251001-v1:0") == [
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-haiku-4-5-20251001-v1",
        "anthropic.claude-haiku-4-5-20251001",
        "anthropic.claude-haiku-4-5",
    ]
    assert identity.id_aliases("openai.gpt-oss-120b-1:0") == [
        "openai.gpt-oss-120b-1:0", "openai.gpt-oss-120b-1", "openai.gpt-oss-120b"]


def test_letter_guard_never_truncates_version_numbers():
    # the load-bearing guard: 4-7 must NOT collapse to 4 (would collide with 4-6)
    assert identity.id_aliases("anthropic.claude-opus-4-7") == ["anthropic.claude-opus-4-7"]
    assert identity.id_aliases("anthropic.claude-sonnet-5") == ["anthropic.claude-sonnet-5"]


def test_regression_no_truncating_suffix_mismatch():
    """Req 2.6: opus-4-7 must never resolve to Claude Opus 4.6 keys, sonnet-5
    never to Claude Sonnet 4 — while gpt-oss-120b-1:0 still aliases down."""
    cp = _fixture("list_foundation_models.json")["modelSummaries"]
    index = identity.build_index(m["modelId"] for m in cp)
    # exact-match lookups of the ids being priced
    assert index.get("anthropic.claude-opus-4-7") == "anthropic.claude-opus-4-7"
    assert index.get("anthropic.claude-sonnet-5") == "anthropic.claude-sonnet-5"
    # catalog-side expansion reaches the bare key from the versioned id
    assert index.get("openai.gpt-oss-120b") == "openai.gpt-oss-120b-1:0"
    # opus-4-6's aliases never claim opus-4-7's key and vice versa
    assert index.get("anthropic.claude-opus-4-6") == "anthropic.claude-opus-4-6-v1"
    assert index["anthropic.claude-opus-4-6"] != "anthropic.claude-opus-4-7"


def test_ambiguous_alias_key_returns_no_match():
    index = identity.build_index(["vendor.model-a-20250101", "vendor.model-a-20260101"])
    assert index["vendor.model-a"] is None  # both reduce here → never guess
    assert index["vendor.model-a-20250101"] == "vendor.model-a-20250101"


def test_name_index_joins_and_refuses_ambiguity():
    cp = _fixture("list_foundation_models.json")["modelSummaries"]
    idx = identity.build_name_index((m["modelId"], m["modelName"]) for m in cp)
    canonical, ids = idx[identity.normalize_name("Claude Opus 5")]
    assert canonical == "anthropic.claude-opus-5" and ids == ("anthropic.claude-opus-5",)
    # context-window variants of one model share a name AND a base → one canonical
    canonical, ids = idx[identity.normalize_name("Claude 3 Haiku")]
    assert canonical == "anthropic.claude-3-haiku-20240307-v1:0"
    assert len(ids) == 3
    # names that normalize differently never join
    assert identity.normalize_name("Claude Opus 4.6") != identity.normalize_name("Claude Opus 4.7")
    assert identity.normalize_name("Claude Sonnet 4") != identity.normalize_name("Claude Sonnet 5")


def test_normalize_name_strips_noise_not_ids():
    assert identity.normalize_name("Qwen3 32B (dense)") == identity.normalize_name("Qwen3-32B")
    assert identity.normalize_name("Gemma 3 12B IT") == identity.normalize_name("Gemma-3-12B-IT")
    assert identity.normalize_name("Claude Opus 5") == "claudeopus5"


# ── rate resolution (Req 1.5, 5.x, 6.x, 7.x / tests 12.1-12.3, 12.8, 12.9) ──

def test_opus5_in_region_resolves_published_dollars():
    """Req 1.5: us-east-1 in-region standard = $5.50 / $27.50 per 1M."""
    rin = resolve_rate(OPUS5, "input")
    rout = resolve_rate(OPUS5, "output")
    assert (rin.usd_per_1m, rin.source) == (5.5, "aws-published")
    assert (rout.usd_per_1m, rout.source) == (27.5, "aws-published")
    assert rin.matched_routing == "in_region" and not rin.fallback
    assert rin.version == "20260728133434"
    assert rin.per_token() == 5.5e-06


def test_override_beats_published_and_reverts_on_delete():
    entry = {
        "override": {"rates": {"input": 4.0, "output": 20.0}, "_UNIT": resolver.UNIT_PER_1M,
                     "scope": "ALL", "updated_at": 1785000000},
        "published": OPUS5["published"],
    }
    r = resolve_rate(entry, "input")
    assert (r.usd_per_1m, r.source) == (4.0, "override")
    assert r.version == "override:1785000000"
    # delete the override → reverts to the published rate
    del entry["override"]
    r = resolve_rate(entry, "input")
    assert (r.usd_per_1m, r.source) == (5.5, "aws-published")


def test_legacy_per_token_override_shape_is_dropped_after_d6():
    # D6 (06-GATEWAY-PRICING-COVERAGE): the flat per-token input/output
    # override tolerance is DELETED — zero live rows use it and the writer
    # never produces it. A row in the removed shape now resolves unpriced
    # rather than being silently lifted; only the current grid contract reads.
    entry = {"override": {"input": 1.5e-05, "output": 7.5e-05}, "published": None}
    r = resolve_rate(entry, "input")
    assert (r.usd_per_1m, r.source) == (None, "unpriced")
    # the current override contract (grid map + _UNIT) still reads
    entry = {"override": {"rates": {"input": 15.0}, "_UNIT": resolver.UNIT_PER_1M},
             "published": None}
    r = resolve_rate(entry, "input")
    assert (r.usd_per_1m, r.source) == (15.0, "override")


def test_absent_model_is_unpriced_never_a_default():
    r = resolve_rate({"override": None, "published": None}, "input")
    assert (r.usd_per_1m, r.source) == (None, "unpriced")
    assert r.per_token() is None


def test_tier_fallback_flex_to_standard_is_flagged():
    r = resolve_rate(OPUS5, "input", tier="flex")
    assert r.usd_per_1m == 5.5 and r.matched_tier == "standard"
    assert r.tier_fallback and r.fallback and not r.routing_fallback


def test_batch_tier_resolves_its_own_rate():
    r = resolve_rate(OPUS5, "input", tier="batch")
    assert r.usd_per_1m == 2.75 and r.matched_tier == "batch" and not r.fallback


def test_routing_modes_price_their_published_rates():
    """Req 12.9: Opus 5 carries a 10% in-region premium; Sonnet 4 is identical."""
    assert resolve_rate(OPUS5, "input", routing="in_region").usd_per_1m == 5.5
    assert resolve_rate(OPUS5, "input", routing="global").usd_per_1m == 5.0
    assert resolve_rate(OPUS5, "output", routing="global").usd_per_1m == 25.0
    s_in = resolve_rate(SONNET4, "input", routing="in_region")
    s_gl = resolve_rate(SONNET4, "input", routing="global")
    assert s_in.usd_per_1m == s_gl.usd_per_1m == 3.0
    assert not s_in.fallback and not s_gl.fallback
    # Haiku 4.5 published +10% in-region over global
    assert resolve_rate(HAIKU45, "input", routing="in_region").usd_per_1m == 1.1
    assert resolve_rate(HAIKU45, "input", routing="global").usd_per_1m == 1.0


def test_geo_routing_prices_in_region_without_fallback_flag():
    """Req 7.4: no on-demand geo token SKU exists — geo maps to in-region.
    Auditable via matched_routing, not flagged as a fallback."""
    r = resolve_rate(OPUS5, "input", routing="geo")
    assert r.usd_per_1m == 5.5 and r.matched_routing == "in_region"
    assert not r.fallback and not r.routing_fallback


def test_future_geo_key_is_preferred_over_in_region():
    """Req 7.5: if AWS ever publishes a geo on-demand rate, it wins with no schema change."""
    entry = {"published": {
        "_UNIT": resolver.UNIT_PER_1M,
        "rates": {"in_region": {"standard": {"default": {"input": 5.5}}},
                  "geo": {"standard": {"default": {"input": 5.2}}}},
    }}
    r = resolve_rate(entry, "input", routing="geo")
    assert r.usd_per_1m == 5.2 and r.matched_routing == "geo" and not r.fallback


def test_global_only_slice_serves_in_region_request_flagged():
    """Req 7.8 / 12.8: a model publishing only a global rate must price an
    in-region request at that rate, recorded as a routing fallback."""
    entry = {"published": {
        "_UNIT": resolver.UNIT_PER_1M,
        "rates": {"global": {"standard": {"default": {"input": 2.0, "output": 8.0}}}},
    }}
    r = resolve_rate(entry, "input", routing="in_region")
    assert r.usd_per_1m == 2.0
    assert r.matched_routing == "global" and r.routing_fallback and r.fallback


def test_long_context_rates_are_separately_addressable():
    """Req 6.5 against the real Sonnet 4 long-context global rows."""
    r = resolve_rate(SONNET4, "input", routing="global", context="long")
    assert r.usd_per_1m == 6.0 and r.matched_context == "long" and not r.fallback
    # default context unaffected
    assert resolve_rate(SONNET4, "input", routing="global").usd_per_1m == 3.0


def test_cache_directions_price_their_own_rates():
    """Req 6.4: cache read/write rates stay separately addressable."""
    assert resolve_rate(OPUS5, "cache_read").usd_per_1m == 0.55
    assert resolve_rate(OPUS5, "cache_write_1h").usd_per_1m == 11.0
    # undated cache write falls back from the duration-specific ask, flagged
    r = resolve_rate(OPUS5, "cache_write_5m")
    assert r.usd_per_1m == 6.875 and r.matched_direction == "cache_write"
    assert r.direction_fallback and r.fallback


def test_per_1m_magnitude_guard():
    """Req 12.5: stored rates are per-1M — a frontier input rate lands in
    dollars-per-1M magnitude, never per-token or per-1K."""
    for entry in (OPUS5, SONNET4, HAIKU45):
        v = resolve_rate(entry, "input").usd_per_1m
        assert 0.01 <= v <= 1000, v


def test_unwrap_item_converts_ddb_types():
    item = {"pk": {"S": "PRICING#m"}, "updated_at": {"N": "1785000000"},
            "partial": {"BOOL": False},
            "rates": {"M": {"in_region": {"M": {"standard": {"M": {"default": {"M": {"input": {"N": "5.5"}}}}}}}}}}
    plain = resolver.unwrap_item(item)
    assert plain["rates"]["in_region"]["standard"]["default"]["input"] == 5.5
    assert plain["updated_at"] == 1785000000.0 and plain["partial"] is False
