# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the metering Lambdas' pure logic (no AWS calls).

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ → import pricing


def _load(name: str, env: dict):
    os.environ.update(env)
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    spec = importlib.util.spec_from_file_location(name, HERE.parent / name / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


debit = _load("debit", {"TABLE": "t", "SNS_TOPIC": ""})


class _FakePricingDdb:
    """batch_get_item double returning seeded PRICING# rows."""

    def __init__(self, rows):
        self.rows = rows  # list of DDB-typed items

    def batch_get_item(self, RequestItems):
        keys = {(k["pk"]["S"], k["sk"]["S"]) for k in RequestItems["t"]["Keys"]}
        hits = [r for r in self.rows if (r["pk"]["S"], r["sk"]["S"]) in keys]
        return {"Responses": {"t": hits}}


def _grid_row(model: str, rates: dict) -> dict:
    def to_attr(v):
        if isinstance(v, dict):
            return {"M": {k: to_attr(x) for k, x in v.items()}}
        return {"N": str(v)}

    return {"pk": {"S": f"PRICING#{model}"}, "sk": {"S": "PUBLISHED"},
            "model_id": {"S": model}, "_UNIT": {"S": "USD/1M-tokens"},
            "offer_version": {"S": "20260728133434"}, "rates": to_attr(rates)}


SONNET5_ROW = _grid_row("anthropic.claude-sonnet-5", {
    "in_region": {"standard": {"default": {"input": 2.2, "output": 11.0}}},
    "global": {"standard": {"default": {"input": 2.0, "output": 10.0}}},
})


def test_catalog_entry_resolves_published_rates(monkeypatch):
    """Replaces the old claude-sonnet-5-is-unpriced assertion (Req 12.10):
    the model is published — $2.20/$11.00 per 1M in-region."""
    monkeypatch.setattr(debit, "ddb", _FakePricingDdb([SONNET5_ROW]))
    debit._catalog_cache.clear()
    entry = debit._catalog_entry("anthropic.claude-sonnet-5")
    rin = debit.resolve_rate(entry, "input")
    rout = debit.resolve_rate(entry, "output")
    assert (rin.per_token(), rin.source) == (2.2e-06, "aws-published")
    assert (rout.per_token(), rout.source) == (1.1e-05, "aws-published")
    # tier falls back to standard, flagged
    flex = debit.resolve_rate(entry, "input", tier="flex")
    assert flex.per_token() == 2.2e-06 and flex.tier_fallback
    # routing follows the request (Req 7.3)
    glb = debit.resolve_rate(entry, "input", routing="global")
    assert glb.usd_per_1m == 2.0 and glb.matched_routing == "global"


def test_absent_model_is_unpriced_never_a_guess(monkeypatch):
    monkeypatch.setattr(debit, "ddb", _FakePricingDdb([]))
    debit._catalog_cache.clear()
    entry = debit._catalog_entry("vendor.never-published")
    r = debit.resolve_rate(entry, "input")
    assert (r.usd_per_1m, r.source) == (None, "unpriced")


def test_override_row_beats_published(monkeypatch):
    override = {"pk": {"S": "PRICING#anthropic.claude-sonnet-5"}, "sk": {"S": "OVERRIDE"},
                "_UNIT": {"S": "USD/1M-tokens"}, "scope": {"S": "ALL"},
                "rates": {"M": {"input": {"N": "1.5"}}}, "updated_at": {"N": "1785000000"}}
    monkeypatch.setattr(debit, "ddb", _FakePricingDdb([SONNET5_ROW, override]))
    debit._catalog_cache.clear()
    entry = debit._catalog_entry("anthropic.claude-sonnet-5")
    r = debit.resolve_rate(entry, "input")
    assert (r.usd_per_1m, r.source) == (1.5, "override")
    # direction the override does not carry falls through to published
    assert debit.resolve_rate(entry, "output").source == "aws-published"


def test_idempotency_key_preference():
    assert debit._idempotency_key({"response_id": "chatcmpl-abc"}) == "resp#chatcmpl-abc"
    assert debit._idempotency_key({"chat_id": "c1", "message_id": "m1"}) == "msg#c1#m1"
    assert debit._idempotency_key({"estimate_key": "h123"}) == "est#h123"
    # response id wins over message ids
    assert debit._idempotency_key({"response_id": "r", "chat_id": "c", "message_id": "m"}) == "resp#r"


def test_month_window():
    # 2026-07-15T00:00:00Z
    assert debit._month_window(1784073600) == "2026-07"


def test_model_normalization_and_routing_derivation():
    """_settle's model cleanup is parse_model_ref: gateway/pipe prefixes strip,
    inference-profile scopes derive routing and share one key (Req 2.9, 12.6)."""
    for raw, want_key, want_routing in [
        ("bedrock/qwen.qwen3-32b", "qwen.qwen3-32b", "in_region"),
        ("gateway_anthropic.anthropic.claude-haiku-4-5", "anthropic.claude-haiku-4-5", "in_region"),
        ("qwen.qwen3-32b", "qwen.qwen3-32b", "in_region"),
        ("global.anthropic.claude-opus-5", "anthropic.claude-opus-5", "global"),
        ("us.anthropic.claude-opus-5", "anthropic.claude-opus-5", "geo"),
        ("bedrock/us.anthropic.claude-opus-5", "anthropic.claude-opus-5", "geo"),
    ]:
        ref = debit.parse_model_ref(raw)
        assert (ref.key, ref.routing) == (want_key, want_routing), raw
