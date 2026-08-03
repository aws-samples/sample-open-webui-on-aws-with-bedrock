# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pricing refresher tests — offer-file parsing, resolution, writes, GC.

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q

Fixtures are trimmed REAL offer files; the refresher runs against an
in-memory DynamoDB double, so every assertion here is offline.
"""

import importlib.util
import json
import os
import pathlib
import sys
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ → import pricing

from pricing import offers  # noqa: E402


def _fixture(name: str) -> dict:
    return json.loads((HERE / "fixtures" / name).read_text(encoding="utf-8"))


# ── offer parser against the real fixtures (task 5.5 / Req 1.2-1.4, 12.5) ──

def test_marketplace_servicename_extraction_and_per_1m():
    rates, version = offers.parse_offer(
        _fixture("offer_foundation_models.json"), "us-east-1", "AmazonBedrockFoundationModels")
    assert version == "20260728133434"
    opus = [r for r in rates if r.identity == "Claude Opus 5"]
    assert opus, "servicename minus ' (Amazon Bedrock Edition)' must identify the model"
    assert all(r.identity_kind == "name" for r in opus)
    std_in = next(r for r in opus if (r.routing, r.tier, r.context, r.direction)
                  == ("in_region", "standard", "default", "input"))
    assert std_in.usd_per_1m == Decimal("5.5")  # published per-1M, stored as-is


def test_tierless_marketplace_usage_type_is_standard():
    rates, _ = offers.parse_offer(
        _fixture("offer_foundation_models.json"), "us-east-1", "AmazonBedrockFoundationModels")
    # CamelCase shapes (InputTokenCount…) name no tier → standard (Req 1.4)
    camel = [r for r in rates if "TokenCount" in r.usagetype and "Batch" not in r.usagetype]
    assert camel and all(r.tier == "standard" for r in camel)


def test_per_1k_normalizes_to_per_1m():
    rates, _ = offers.parse_offer(_fixture("offer_bedrock.json"), "us-east-1", "AmazonBedrock")
    qwen_in = next(r for r in rates if r.identity == "qwen.qwen3-32b"
                   and (r.tier, r.direction) == ("standard", "input"))
    # published $0.00015/1K → $0.15/1M (Req 1.3)
    assert qwen_in.usd_per_1m == Decimal("0.15")
    assert qwen_in.identity_kind == "id"
    legacy = next(r for r in rates if r.identity == "Claude 3 Haiku")
    assert legacy.usd_per_1m == Decimal("0.25") and legacy.identity_kind == "name"


def test_cross_region_global_shapes_classify_global():
    rates, _ = offers.parse_offer(_fixture("offer_service.json"), "us-east-1", "AmazonBedrockService")
    assert rates and all(r.routing == "global" for r in rates)
    long_ctx = [r for r in rates if r.context == "long"]
    assert long_ctx, "long-context rows must be retained as separately addressable"


def test_parsed_magnitude_guard_per_1m():
    for fx, svc in [("offer_foundation_models.json", "AmazonBedrockFoundationModels"),
                    ("offer_bedrock.json", "AmazonBedrock"),
                    ("offer_service.json", "AmazonBedrockService")]:
        rates, _ = offers.parse_offer(_fixture(fx), "us-east-1", svc)
        for r in rates:
            assert Decimal("0.001") <= r.usd_per_1m <= Decimal("1000"), (r.usagetype, r.usd_per_1m)


# ── refresher end-to-end against fakes (task 6.9 / Req 4.6, 10.1, 10.2) ──

class FakeDdb:
    """Minimal DynamoDB double for the refresher's access patterns."""

    def __init__(self):
        self.items: dict = {}  # (pk, sk) -> item

    def seed(self, item: dict):
        self.items[(item["pk"]["S"], item["sk"]["S"])] = item

    def put_item(self, TableName=None, Item=None):
        self.seed(Item)
        return {}

    def delete_item(self, TableName=None, Key=None):
        self.items.pop((Key["pk"]["S"], Key["sk"]["S"]), None)
        return {}

    def get_item(self, TableName=None, Key=None, **kw):
        item = self.items.get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": item} if item else {}

    def query(self, TableName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, **kw):
        pk = ExpressionAttributeValues[":p"]["S"]
        return {"Items": [it for (p, _), it in sorted(self.items.items()) if p == pk]}

    def scan(self, TableName=None, FilterExpression=None,
             ExpressionAttributeValues=None, **kw):
        prefix = ExpressionAttributeValues[":p"]["S"]
        return {"Items": [it for (p, _), it in sorted(self.items.items()) if p.startswith(prefix)]}


def _load_refresher():
    os.environ.update({
        "TABLE": "t", "REGION": "us-east-1",
        # fake static creds so botocore's env provider wins before any
        # profile-based provider (keeps module import offline on dev machines)
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    })
    os.environ.pop("AWS_PROFILE", None)
    spec = importlib.util.spec_from_file_location(
        "pricing_refresher", HERE.parent / "pricing-refresher" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pricing_refresher"] = mod
    spec.loader.exec_module(mod)
    return mod


FIXTURE_BY_SERVICE = {
    "AmazonBedrockFoundationModels": "offer_foundation_models.json",
    "AmazonBedrock": "offer_bedrock.json",
    "AmazonBedrockService": "offer_service.json",
}
CP_MODELS = [(m["modelId"], m["modelName"], m["providerName"])
             for m in _fixture("list_foundation_models.json")["modelSummaries"]]


def _run(mod, fake, fail_services=(), cp=CP_MODELS):
    mod.ddb = fake
    mod._metric = lambda *a, **k: None
    mod._list_cp_models = lambda: cp

    def fetch(svc):
        if svc in fail_services:
            raise OSError(f"{svc} unavailable")
        return _fixture(FIXTURE_BY_SERVICE[svc])

    mod._fetch_offer = fetch
    return mod.handler({}, None)


def _published(fake):
    return {pk.removeprefix("PRICING#"): it for (pk, sk), it in fake.items.items()
            if sk == "PUBLISHED"}


def test_refresh_writes_model_id_keys_with_published_grid():
    mod = _load_refresher()
    fake = FakeDdb()
    out = _run(mod, fake)
    assert out["ok"] and not out["partial"]
    pub = _published(fake)
    # Req 1.5: Opus 5 keyed by its Bedrock model id at the published rate
    opus = pub["anthropic.claude-opus-5"]
    rates = opus["rates"]["M"]
    std = rates["in_region"]["M"]["standard"]["M"]["default"]["M"]
    assert std["input"]["N"] == "5.5" and std["output"]["N"] == "27.5"
    glb = rates["global"]["M"]["standard"]["M"]["default"]["M"]
    assert glb["input"]["N"] == "5" and glb["output"]["N"] == "25"
    assert opus["resolved_via"]["S"] == "control-plane-name"
    assert opus["_UNIT"]["S"] == "USD/1M-tokens"
    assert opus["offer_version"]["S"] == "20260728133434"
    # dated CP ids materialize alias keys so every invocable id resolves (Req 2.9)
    haiku_keys = {k for k in pub if k.startswith("anthropic.claude-haiku-4-5")}
    assert haiku_keys == {
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-haiku-4-5-20251001-v1",
        "anthropic.claude-haiku-4-5-20251001",
        "anthropic.claude-haiku-4-5",
    }
    assert pub["anthropic.claude-haiku-4-5"]["alias_of"]["S"] == "anthropic.claude-haiku-4-5-20251001-v1:0"
    # mantle id and its control-plane twin collapse to ONE model (merge)
    assert pub["openai.gpt-oss-120b"]["canonical_id"]["S"] == "openai.gpt-oss-120b"
    assert pub["openai.gpt-oss-120b-1:0"]["canonical_id"]["S"] == "openai.gpt-oss-120b"
    # every written key is a valid model id
    for key in pub:
        assert mod.identity.MODEL_ID_RE.match(key), key


def test_unmatched_names_recorded_not_priced():
    mod = _load_refresher()
    fake = FakeDdb()
    cp = [m for m in CP_MODELS if m[1] != "Claude 3 Haiku"]  # remove the CP twin
    _run(mod, fake, cp=cp)
    unmatched = {sk: it for (pk, sk), it in fake.items.items() if pk == "PRICING#_UNMATCHED"}
    assert "Claude 3 Haiku" in unmatched
    row = unmatched["Claude 3 Haiku"]
    assert row["reason"]["S"] == "no-control-plane-match"
    cand = row["candidate_rates"]["M"]["in_region"]["M"]["standard"]["M"]["default"]["M"]
    assert cand["input"]["N"] == "0.25"
    # and no PUBLISHED row exists for it under any key (Req 3.2)
    assert not any("haiku" in k.lower() and "3" in k for k in _published(fake))


def test_operator_alias_binds_and_outranks_inference():
    mod = _load_refresher()
    fake = FakeDdb()
    fake.seed({"pk": {"S": "PRICING#_ALIAS"}, "sk": {"S": "Claude 3 Haiku"},
               "model_id": {"S": "anthropic.claude-3-haiku-20240307-v1:0"}})
    _run(mod, fake, cp=[m for m in CP_MODELS if m[1] != "Claude 3 Haiku"])
    pub = _published(fake)
    row = pub["anthropic.claude-3-haiku-20240307-v1:0"]
    assert row["resolved_via"]["S"] == "alias"
    # the binding resolves what was unmatched → the review row is gone (Req 3.4)
    assert ("PRICING#_UNMATCHED", "Claude 3 Haiku") not in fake.items


def test_gc_collects_legacy_keys_and_removed_tiers_preserving_overrides():
    mod = _load_refresher()
    fake = FakeDdb()
    fake.seed({"pk": {"S": "PRICING#Claude4Sonnet"}, "sk": {"S": "PUBLISHED"},
               "input": {"N": "3e-06"}})  # legacy display-token key (Req 10.1)
    fake.seed({"pk": {"S": "PRICING#anthropic.claude-sonnet-5"}, "sk": {"S": "PROVIDER"},
               "source": {"S": "provider-list"}})  # removed tier (Req 9.4)
    fake.seed({"pk": {"S": "PRICING#anthropic.claude-sonnet-5"}, "sk": {"S": "DEFAULT"},
               "source": {"S": "default-override"}})
    fake.seed({"pk": {"S": "PRICING#anthropic.claude-sonnet-5"}, "sk": {"S": "OVERRIDE"},
               "input": {"N": "3e-06"}, "note": {"S": "operator"}})  # NEVER touched
    fake.seed({"pk": {"S": "PRICING#vendor.stale-model"}, "sk": {"S": "PUBLISHED"},
               "model_id": {"S": "vendor.stale-model"}})  # absent from this run
    out = _run(mod, fake)
    assert ("PRICING#Claude4Sonnet", "PUBLISHED") not in fake.items
    assert ("PRICING#anthropic.claude-sonnet-5", "PROVIDER") not in fake.items
    assert ("PRICING#anthropic.claude-sonnet-5", "DEFAULT") not in fake.items
    assert ("PRICING#vendor.stale-model", "PUBLISHED") not in fake.items
    assert ("PRICING#anthropic.claude-sonnet-5", "OVERRIDE") in fake.items  # Req 10.2
    assert out["gc"]["legacy_key"] == 1 and out["gc"]["provider_default"] == 2


def test_partial_fetch_never_deletes_stale_rows():
    mod = _load_refresher()
    fake = FakeDdb()
    fake.seed({"pk": {"S": "PRICING#vendor.stale-model"}, "sk": {"S": "PUBLISHED"},
               "model_id": {"S": "vendor.stale-model"}})
    out = _run(mod, fake, fail_services=("AmazonBedrockService",))
    assert out["partial"] and out["failed_services"] == ["AmazonBedrockService"]
    # the stale-but-valid row survives a partial refresh (Req 4.6)
    assert ("PRICING#vendor.stale-model", "PUBLISHED") in fake.items
    # …but unreadable legacy keys are still collected unconditionally
    meta = fake.items[("PRICING#_CATALOG", "META")]
    assert meta["partial"]["BOOL"] is True


def test_rerun_is_idempotent_apart_from_generation_and_timestamps():
    mod = _load_refresher()
    fake = FakeDdb()
    _run(mod, fake)
    first = {k: dict(v) for k, v in fake.items.items()}
    _run(mod, fake)

    def strip(item):
        return {k: v for k, v in item.items()
                if k not in ("refresh_generation", "updated_at", "refreshed_at", "duration_ms")}

    assert set(fake.items) == set(first)
    for key, item in fake.items.items():
        assert strip(item) == strip(first[key]), key
    gen1 = int(first[("PRICING#_CATALOG", "META")]["refresh_generation"]["N"])
    gen2 = int(fake.items[("PRICING#_CATALOG", "META")]["refresh_generation"]["N"])
    assert gen2 == gen1 + 1


def test_all_files_failing_raises_and_leaves_rows():
    import pytest
    mod = _load_refresher()
    fake = FakeDdb()
    fake.seed({"pk": {"S": "PRICING#anthropic.claude-opus-5"}, "sk": {"S": "PUBLISHED"},
               "model_id": {"S": "anthropic.claude-opus-5"}})
    with pytest.raises(RuntimeError):
        _run(mod, fake, fail_services=tuple(FIXTURE_BY_SERVICE))
    assert ("PRICING#anthropic.claude-opus-5", "PUBLISHED") in fake.items
