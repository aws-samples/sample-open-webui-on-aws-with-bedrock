# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Coverage-join tests (contract §2, §3, §8; design D1/D2/D3).

The refresher's gateway↔pricing coverage join: universe = union(served
MODEL_CAPS, live catalog); per-model listed/available/priced + counts; the
UnpricedGatewayModels metric; and the MODEL_CAPS fallback when the model
refresher is disabled. Offline — every AWS seam is stubbed.

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # metering/ → import pricing


def _load_refresher():
    os.environ.update({
        "TABLE": "t", "REGION": "us-east-1", "MANTLE_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    })
    os.environ.pop("AWS_PROFILE", None)
    os.environ.pop("INTERCEPTOR_FUNCTION_NAME", None)
    spec = importlib.util.spec_from_file_location(
        "pricing_refresher_cov", HERE.parent / "pricing-refresher" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pricing_refresher_cov"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── _build_coverage: (listed × available × priced) combinations (contract §2) ──

def _priced_stub(priced_ids):
    """A _priced() that reports the given ids as aws-published-priced.

    Signature tracks the live fix: coverage now consults OVERRIDE rows
    (override_row kwarg) — see test_live_regressions for the override path.
    """
    def fn(resolved, keys_by_canonical, key, override_row=None):
        return (key in priced_ids, "aws-published" if key in priced_ids else None)
    return fn


def _cov(mod, caps, catalog_ids, priced_ids, catalog_error=None):
    mod._priced = _priced_stub(priced_ids)
    mod._override_rows = lambda keys: {}  # override reads covered elsewhere
    return mod._build_coverage({}, {}, caps, set(catalog_ids), catalog_error)


def test_listed_available_priced_is_ok_and_counted_invokable_priced():
    mod = _load_refresher()
    cov = _cov(mod, {"chat_completions": ["a.m1"]}, ["a.m1"], {"a.m1"})
    m = {x["id"]: x for x in cov["models"]}["a.m1"]
    assert m["listed"] and m["catalog_available"] and m["priced"]
    assert m["reason"] == "ok" and m["lanes"] == ["chat_completions"]
    assert cov["counts"] == {"invokable": 1, "invokable_priced": 1,
                             "invokable_unpriced": 0, "listed_not_available": 0,
                             "invokable_not_in_control_plane": 0}


def test_available_unpriced_is_no_pricing_row_and_invokable_unpriced():
    mod = _load_refresher()
    cov = _cov(mod, {"chat_completions": ["a.m1"]}, ["a.m1"], set())
    m = {x["id"]: x for x in cov["models"]}["a.m1"]
    assert m["catalog_available"] and not m["priced"]
    assert m["reason"] == "no-pricing-row" and m["source"] is None
    assert cov["counts"]["invokable_unpriced"] == 1


def test_unlisted_but_available_is_invokable_listed_false():
    mod = _load_refresher()
    # a catalog model in NO lane is still invokable by a crafted request (D2)
    cov = _cov(mod, {"chat_completions": []}, ["a.rogue"], {"a.rogue"})
    m = {x["id"]: x for x in cov["models"]}["a.rogue"]
    assert m["catalog_available"] and not m["listed"] and m["lanes"] == []
    assert m["priced"] and m["reason"] == "ok"
    assert cov["counts"]["invokable"] == 1 and cov["counts"]["invokable_priced"] == 1


def test_listed_not_available_is_stale_caps():
    mod = _load_refresher()
    cov = _cov(mod, {"messages": ["a.gone"]}, [], set())
    m = {x["id"]: x for x in cov["models"]}["a.gone"]
    assert m["listed"] and not m["catalog_available"]
    assert m["reason"] == "stale-caps"
    assert cov["counts"] == {"invokable": 0, "invokable_priced": 0,
                             "invokable_unpriced": 0, "listed_not_available": 1,
                             "invokable_not_in_control_plane": 0}


def test_null_rates_when_only_one_direction_prices():
    mod = _load_refresher()
    # _priced returns (False, "aws-published") ⇒ a row exists but a rate is null
    mod._priced = lambda r, k, key, override_row=None: (False, "aws-published")
    cov = mod._build_coverage({}, {}, {"chat_completions": ["a.m1"]}, {"a.m1"}, None)
    m = {x["id"]: x for x in cov["models"]}["a.m1"]
    assert m["reason"] == "null-rates" and not m["priced"]
    assert cov["counts"]["invokable_unpriced"] == 1


def test_universe_is_union_of_caps_and_catalog():
    mod = _load_refresher()
    cov = _cov(mod, {"chat_completions": ["a.listed"]}, ["a.catalog"], set())
    ids = {m["id"] for m in cov["models"]}
    assert ids == {"a.listed", "a.catalog"}


# ── THE regression guard: gpt-5.6 publishing gap (design baseline) ───────────

def test_gpt56_publishing_gap_regression_drives_unpriced_gateway_models():
    """A model present in caps AND the live catalog but with NO pricing row
    surfaces as invokable_unpriced with reason=no-pricing-row and drives
    UnpricedGatewayModels >= 1 — never a silent drop (design D3 baseline: the
    GPT-5.x/mantle family is an AWS publishing gap, not a parser defect)."""
    mod = _load_refresher()
    gap_model = "openai.gpt-5.6-sol"
    caps = {"responses": [gap_model, "openai.gpt-oss-120b"]}
    catalog = [gap_model, "openai.gpt-oss-120b"]
    # only gpt-oss-120b is priced; the gap model has no row
    cov = _cov(mod, caps, catalog, {"openai.gpt-oss-120b"})

    gap = {m["id"]: m for m in cov["models"]}[gap_model]
    assert gap["listed"] and gap["catalog_available"]
    assert not gap["priced"]
    assert gap["reason"] == "no-pricing-row"
    assert gap["source"] is None
    assert cov["counts"]["invokable_unpriced"] >= 1

    # …and the emitted gauge is the invokable_unpriced count (alarm >= 1).
    emitted = {}
    mod._metric = lambda name, value=1, unit="Count": emitted.__setitem__(name, value)
    mod._emit_coverage_metrics(cov, {}, 0, 0)
    assert emitted["UnpricedGatewayModels"] >= 1


# ── MODEL_CAPS fallback (contract §8): works in BOTH enableModelRefresh states ─

def test_served_caps_reads_interceptor_env_when_present():
    mod = _load_refresher()
    mod.INTERCEPTOR_FUNCTION_NAME = "interceptor-fn"
    served = {"chat_completions": ["a.m1"], "responses": [], "messages": []}

    class FakeLambda:
        def get_function_configuration(self, FunctionName=None):
            assert FunctionName == "interceptor-fn"
            return {"Environment": {"Variables": {"MODEL_CAPS": json.dumps(served)}}}

    mod._lambda = FakeLambda()
    assert mod._served_caps() == served


def test_served_caps_falls_back_to_bundled_when_env_unset():
    """Model refresher disabled ⇒ interceptor MODEL_CAPS env is unset; the join
    falls back to the packaged capability matrix the interceptor itself uses."""
    mod = _load_refresher()
    mod.INTERCEPTOR_FUNCTION_NAME = "interceptor-fn"
    bundled = {"chat_completions": ["a.bundled"], "responses": [], "messages": []}
    mod._bundled_caps = lambda: bundled

    class FakeLambda:
        def get_function_configuration(self, FunctionName=None):
            return {"Environment": {"Variables": {}}}  # MODEL_CAPS absent

    mod._lambda = FakeLambda()
    assert mod._served_caps() == bundled


def test_served_caps_falls_back_to_bundled_when_no_interceptor_configured():
    mod = _load_refresher()
    mod.INTERCEPTOR_FUNCTION_NAME = ""
    bundled = {"chat_completions": ["a.bundled"]}
    mod._bundled_caps = lambda: bundled
    assert mod._served_caps() == bundled


def test_served_caps_degrades_to_bundled_on_lambda_error():
    mod = _load_refresher()
    mod.INTERCEPTOR_FUNCTION_NAME = "interceptor-fn"
    mod._bundled_caps = lambda: {"messages": ["a.bundled"]}

    class BoomLambda:
        def get_function_configuration(self, FunctionName=None):
            raise RuntimeError("access denied")

    mod._lambda = BoomLambda()
    assert mod._served_caps() == {"messages": ["a.bundled"]}


# ── catalog fetch failure → partial coverage with error, metrics still emit ───

def test_catalog_error_records_partial_coverage_and_still_counts_listed():
    mod = _load_refresher()
    caps = {"messages": ["a.listed"]}
    cov = mod._build_coverage({}, {}, caps, set(), "TimeoutError: boom")
    assert cov["catalog_error"] == "TimeoutError: boom"
    # with no catalog, a listed model is stale-caps (not available)
    m = {x["id"]: x for x in cov["models"]}["a.listed"]
    assert m["reason"] == "stale-caps"
    emitted = {}
    mod._metric = lambda name, value=1, unit="Count": emitted.__setitem__(name, value)
    mod._emit_coverage_metrics(cov, {}, 0, 0)
    assert "UnpricedGatewayModels" in emitted  # metrics emit from what is known
