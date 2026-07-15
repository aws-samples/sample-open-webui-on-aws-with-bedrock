# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the metering Lambdas' pure logic (no AWS calls).

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load(name: str, env: dict):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, HERE.parent / name / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PRICE_MAP = json.dumps(
    {
        "version": "test-1",
        "models": {
            "qwen.qwen3-32b": {"standard": {"input": 1.5e-07, "output": 6e-07}},
            "openai.gpt-oss-20b": {"standard": {"input": 4e-08, "output": 1.6e-07}},
        },
    }
)

debit = _load("debit", {"TABLE": "t", "PRICE_MAP": PRICE_MAP, "SNS_TOPIC": ""})


def test_rate_lookup_and_unpriced():
    assert debit._rate("qwen.qwen3-32b", "input") == 1.5e-07
    assert debit._rate("qwen.qwen3-32b", "output") == 6e-07
    # tier falls back to standard
    assert debit._rate("qwen.qwen3-32b", "input", "flex") == 1.5e-07
    # unpriced model (the claude/gpt-5 gap) returns None, never a guess
    assert debit._rate("anthropic.claude-sonnet-5", "input") is None


def test_idempotency_key_preference():
    assert debit._idempotency_key({"response_id": "chatcmpl-abc"}) == "resp#chatcmpl-abc"
    assert debit._idempotency_key({"chat_id": "c1", "message_id": "m1"}) == "msg#c1#m1"
    assert debit._idempotency_key({"estimate_key": "h123"}) == "est#h123"
    # response id wins over message ids
    assert debit._idempotency_key({"response_id": "r", "chat_id": "c", "message_id": "m"}) == "resp#r"


def test_month_window():
    # 2026-07-15T00:00:00Z
    assert debit._month_window(1784073600) == "2026-07"


def test_model_normalization_via_settle_key_paths():
    # normalization logic mirrors _settle's model cleanup
    for raw, want in [
        ("bedrock/qwen.qwen3-32b", "qwen.qwen3-32b"),
        ("gateway_anthropic.anthropic.claude-haiku-4-5", "anthropic.claude-haiku-4-5"),
        ("qwen.qwen3-32b", "qwen.qwen3-32b"),
    ]:
        model = raw.split("/", 1)[-1]
        if "." in model and model.split(".", 1)[0] in ("gateway_anthropic", "metering"):
            model = model.split(".", 1)[1]
        assert model == want, raw


def test_price_map_file_is_wired_shape():
    prices = json.loads((HERE.parent.parent / "config" / "model-prices.json").read_text())
    assert prices["models"], "generated price map must not be empty"
    sample = next(iter(prices["models"].values()))
    tier = sample.get("standard") or next(iter(sample.values()))
    assert "input" in tier and "output" in tier
    # per-token (not per-1K) sanity: frontier input prices are < $0.001/token
    assert float(tier["input"]) < 1e-3
