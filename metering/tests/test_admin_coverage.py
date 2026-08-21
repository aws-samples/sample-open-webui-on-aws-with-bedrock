# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Admin API coverage-surface tests (contract §5).

GET /pricing/coverage; the _catalog gateway block + effective grid + unpriced-
but-invokable rows; _pricing_meta model_id_pattern + coverage; and the override
'note' roundtrip. Offline — a get/put/delete/query/batch DynamoDB double.

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _load():
    os.environ.update({"TABLE": "t", "USER_POOL_ID": "us-east-1_test", "ALERTS_TOPIC_ARN": ""})
    spec = importlib.util.spec_from_file_location(
        "admin_api_cov", HERE.parent / "admin-api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admin_api_cov"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeDdb:
    """get/put/delete/query/batch_get double."""

    def __init__(self):
        self.items = {}

    def get_item(self, TableName=None, Key=None, **kw):
        it = self.items.get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": it} if it else {}

    def put_item(self, TableName=None, Item=None, **kw):
        self.items[(Item["pk"]["S"], Item["sk"]["S"])] = Item
        return {}

    def delete_item(self, TableName=None, Key=None, **kw):
        self.items.pop((Key["pk"]["S"], Key["sk"]["S"]), None)
        return {}

    def query(self, TableName=None, KeyConditionExpression=None,
              ExpressionAttributeValues=None, **kw):
        pk = ExpressionAttributeValues[":p"]["S"]
        return {"Items": [it for (p, _), it in sorted(self.items.items()) if p == pk]}

    def scan(self, TableName=None, FilterExpression=None, ExpressionAttributeValues=None, **kw):
        prefix = ExpressionAttributeValues[":p"]["S"]
        return {"Items": [it for (p, _), it in sorted(self.items.items()) if p.startswith(prefix)]}

    def batch_get_item(self, RequestItems=None, **kw):
        table, spec = next(iter(RequestItems.items()))
        out = []
        for k in spec["Keys"]:
            it = self.items.get((k["pk"]["S"], k["sk"]["S"]))
            if it:
                out.append(it)
        return {"Responses": {table: out}}


def _published(mid, canonical=None, rin="5", rout="25", **extra):
    leaf = {"M": {"input": {"N": rin}, "output": {"N": rout}}}
    rates = {"M": {"in_region": {"M": {"standard": {"M": {"default": leaf}}}}}}
    item = {
        "pk": {"S": f"PRICING#{mid}"}, "sk": {"S": "PUBLISHED"},
        "model_id": {"S": mid}, "canonical_id": {"S": canonical or mid},
        "display_name": {"S": mid}, "provider": {"S": "vendor"},
        "source": {"S": "aws-published"}, "_UNIT": {"S": "USD/1M-tokens"},
        "rates": rates,
    }
    item.update(extra)
    return item


def _meta(model_keys, coverage_models=None):
    return {
        "pk": {"S": "PRICING#_CATALOG"}, "sk": {"S": "META"},
        "refresh_generation": {"N": "5"},
        "offer_versions": {"M": {"AmazonBedrock": {"S": "v1"}}},
        "region": {"S": "us-east-1"}, "refreshed_at": {"N": "1000"},
        "model_keys": {"L": [{"S": k} for k in model_keys]},
        "unmatched_names": {"L": []},
    }


def _coverage_item(models):
    counts = {"invokable": 0, "invokable_priced": 0, "invokable_unpriced": 0,
              "listed_not_available": 0}
    for m in models:
        if m["catalog_available"]:
            counts["invokable"] += 1
            counts["invokable_priced" if m["priced"] else "invokable_unpriced"] += 1
        elif m["listed"]:
            counts["listed_not_available"] += 1
    return {
        "pk": {"S": "PRICING#_COVERAGE"}, "sk": {"S": "META"},
        "computed_at": {"N": "2000"},
        "counts": {"M": {k: {"N": str(v)} for k, v in counts.items()}},
        "models": {"L": [{"M": {
            "id": {"S": m["id"]}, "lanes": {"L": [{"S": ln} for ln in m["lanes"]]},
            "listed": {"BOOL": m["listed"]},
            "catalog_available": {"BOOL": m["catalog_available"]},
            "priced": {"BOOL": m["priced"]},
            "source": ({"S": m["source"]} if m["source"] else {"NULL": True}),
            "reason": {"S": m["reason"]},
        }} for m in models]},
    }


# ── GET /pricing/coverage (contract §5) ──────────────────────────────────────

def test_coverage_endpoint_returns_item_verbatim():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    fake.put_item(Item=_coverage_item([
        {"id": "a.m1", "lanes": ["chat_completions"], "listed": True,
         "catalog_available": True, "priced": True, "source": "aws-published",
         "reason": "ok"}]))
    status, body = mod._coverage()
    assert status == 200
    assert body["counts"]["invokable_priced"] == 1
    assert body["models"][0]["id"] == "a.m1"


def test_coverage_endpoint_404_when_absent():
    mod = _load()
    mod.ddb = FakeDdb()
    status, body = mod._coverage()
    assert status == 404 and "error" in body


def test_coverage_route_sets_cache_header():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    mod._read_cache.clear()
    fake.put_item(Item=_coverage_item([
        {"id": "a.m1", "lanes": [], "listed": False, "catalog_available": True,
         "priced": True, "source": "aws-published", "reason": "ok"}]))
    event = {"routeKey": "GET /pricing/coverage",
             "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u1", "cognito:groups": "[admin]"}}}}}
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 200
    assert resp["headers"].get("Cache-Control") == "private, max-age=30"


# ── _catalog gateway block + effective_grid + coverage-only rows (§5) ─────────

def test_catalog_row_carries_gateway_block_and_effective_grid():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    fake.put_item(Item=_published("a.m1"))
    fake.put_item(Item=_meta(["a.m1"]))
    fake.put_item(Item=_coverage_item([
        {"id": "a.m1", "lanes": ["chat_completions", "responses"], "listed": True,
         "catalog_available": True, "priced": True, "source": "aws-published",
         "reason": "ok"}]))
    cat = mod._catalog()
    row = {m["model"]: m for m in cat["models"]}["a.m1"]
    assert row["gateway"] == {"available": True, "listed": True,
                              "lanes": ["chat_completions", "responses"]}
    # effective_grid is routing → direction → per-1M
    assert row["effective_grid"]["in_region"]["input"] == 5.0
    assert row["effective_grid"]["in_region"]["output"] == 25.0
    # meta carries the coverage counts + computed_at + per-model list
    assert cat["meta"]["coverage"]["invokable_priced"] == 1
    assert cat["meta"]["coverage"]["computed_at"] == 2000
    assert cat["meta"]["coverage"]["models"][0]["id"] == "a.m1"


def test_catalog_includes_coverage_only_unpriced_model_with_source_none():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    # a publishing-gap model: catalog-available, no PUBLISHED/OVERRIDE row
    fake.put_item(Item=_meta([]))  # no priced models
    fake.put_item(Item=_coverage_item([
        {"id": "openai.gpt-5.6-sol", "lanes": ["responses"], "listed": True,
         "catalog_available": True, "priced": False, "source": None,
         "reason": "no-pricing-row"}]))
    cat = mod._catalog()
    row = {m["model"]: m for m in cat["models"]}["openai.gpt-5.6-sol"]
    assert row["effective"]["source"] == "unpriced"
    assert row["gateway"]["available"] is True and row["gateway"]["listed"] is True
    assert row["rates"] is None


def test_pricing_rows_uses_batchget_not_scan_when_meta_present():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    fake.scan = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Scan must not run with meta present"))
    fake.put_item(Item=_published("a.m1"))
    fake.put_item(Item=_meta(["a.m1"]))
    rows = mod._pricing_rows()
    pks = {(it["pk"]["S"], it["sk"]["S"]) for it in rows}
    assert ("PRICING#a.m1", "PUBLISHED") in pks
    assert ("PRICING#_CATALOG", "META") in pks


def test_pricing_rows_scan_fallback_when_meta_absent():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    fake.put_item(Item=_published("a.m1"))  # no _CATALOG meta
    rows = mod._pricing_rows()
    pks = {(it["pk"]["S"], it["sk"]["S"]) for it in rows}
    assert ("PRICING#a.m1", "PUBLISHED") in pks


# ── _pricing_meta: model_id_pattern + coverage (contract §5) ─────────────────

def test_pricing_meta_exposes_model_id_pattern_and_coverage():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    fake.put_item(Item=_meta(["a.m1"]))
    fake.put_item(Item=_coverage_item([
        {"id": "a.m1", "lanes": [], "listed": False, "catalog_available": True,
         "priced": False, "source": None, "reason": "no-pricing-row"}]))
    meta = mod._pricing_meta()
    assert meta["model_id_pattern"] == mod.MODEL_ID_RE.pattern
    assert meta["coverage"]["invokable_unpriced"] == 1
    assert meta["coverage"]["computed_at"] == 2000


def test_pricing_meta_coverage_null_when_absent():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    fake.put_item(Item=_meta(["a.m1"]))
    meta = mod._pricing_meta()
    assert meta["coverage"] is None
    assert meta["model_id_pattern"] == mod.MODEL_ID_RE.pattern


def test_pricing_meta_pattern_present_even_with_no_catalog():
    mod = _load()
    mod.ddb = FakeDdb()
    meta = mod._pricing_meta()
    assert meta["model_id_pattern"] == mod.MODEL_ID_RE.pattern
    assert meta["coverage"] is None


# ── override 'note' persisted + returned in the catalog (contract §5) ────────

def test_override_note_roundtrips_into_catalog():
    mod = _load()
    fake = FakeDdb()
    mod.ddb = fake
    mod._audit = lambda *a, **k: None
    mod._put_price_override(
        "openai.gpt-5.6-sol",
        {"input": 1.25, "output": 10.0, "note": "AWS model-card 272K std, 2026-08"},
        actor="admin1")
    stored = fake.items[("PRICING#openai.gpt-5.6-sol", "OVERRIDE")]
    assert stored["note"]["S"] == "AWS model-card 272K std, 2026-08"
    # and it appears on the catalog row's override object
    fake.put_item(Item=_meta([]))
    cat = mod._catalog()
    row = {m["model"]: m for m in cat["models"]}["openai.gpt-5.6-sol"]
    assert row["override"]["note"] == "AWS model-card 272K std, 2026-08"
    assert row["effective"]["source"] == "override"
    assert row["effective"]["input_per_1m"] == 1.25
