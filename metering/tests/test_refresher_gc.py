# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Refresher GC (D9) + coverage-item write tests.

D9: garbage collection diffs the previous catalog-meta model-key list against
the current run (targeted deletes) and only Scans when the prior meta is
absent — the diff and the Scan must agree on which rows go. Also covers the
PRICING#_COVERAGE item write shape (contract §2) and the _priced() resolver
bridge.

Run: uv run --no-project --with pytest --with boto3 pytest metering/tests/ -q
"""

import importlib.util
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _load_refresher():
    os.environ.update({
        "TABLE": "t", "REGION": "us-east-1", "MANTLE_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    })
    os.environ.pop("AWS_PROFILE", None)
    os.environ.pop("INTERCEPTOR_FUNCTION_NAME", None)
    spec = importlib.util.spec_from_file_location(
        "pricing_refresher_gc", HERE.parent / "pricing-refresher" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pricing_refresher_gc"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeDdb:
    """Records scan/batch/delete calls so equivalence can be asserted."""

    def __init__(self):
        self.items: dict = {}
        self.scans = 0

    def seed(self, item):
        self.items[(item["pk"]["S"], item["sk"]["S"])] = item

    def put_item(self, TableName=None, Item=None, **kw):
        self.seed(Item)
        return {}

    def delete_item(self, TableName=None, Key=None, **kw):
        self.items.pop((Key["pk"]["S"], Key["sk"]["S"]), None)
        return {}

    def get_item(self, TableName=None, Key=None, **kw):
        it = self.items.get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": it} if it else {}

    def scan(self, TableName=None, FilterExpression=None, ExpressionAttributeValues=None, **kw):
        self.scans += 1
        prefix = ExpressionAttributeValues[":p"]["S"]
        return {"Items": [it for (p, _), it in sorted(self.items.items()) if p.startswith(prefix)]}


def _pub(mid):
    return {"pk": {"S": f"PRICING#{mid}"}, "sk": {"S": "PUBLISHED"}, "model_id": {"S": mid}}


# ── D9: targeted diff vs. Scan agree on stale deletions ──────────────────────

def test_gc_targeted_diff_deletes_only_prior_keys_absent_this_run_no_scan():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    fake.seed(_pub("a.kept"))
    fake.seed(_pub("a.stale"))  # in prior list, not written this run
    stats = mod._gc(
        written_keys={"a.kept"}, current_unmatched=set(), full_success=True,
        prior_keys=["a.kept", "a.stale"], prior_unmatched=[])
    assert ("PRICING#a.stale", "PUBLISHED") not in fake.items
    assert ("PRICING#a.kept", "PUBLISHED") in fake.items
    assert stats["stale"] == 1
    assert fake.scans == 0  # targeted path never Scans (D9)


def test_gc_targeted_diff_deletes_stale_unmatched_by_prior_list():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    fake.seed({"pk": {"S": "PRICING#_UNMATCHED"}, "sk": {"S": "Gone Name"}})
    mod._gc(written_keys={"a.kept"}, current_unmatched=set(), full_success=True,
            prior_keys=["a.kept"], prior_unmatched=["Gone Name"])
    assert ("PRICING#_UNMATCHED", "Gone Name") not in fake.items


def test_gc_scan_fallback_when_prior_meta_absent_matches_targeted_result():
    mod = _load_refresher()
    # Scan path (prior_keys=None): stale valid model-id row is collected too.
    fake = FakeDdb()
    mod.ddb = fake
    fake.seed(_pub("a.kept"))
    fake.seed(_pub("a.stale"))
    stats = mod._gc(written_keys={"a.kept"}, current_unmatched=set(), full_success=True,
                    prior_keys=None, prior_unmatched=None)
    assert fake.scans == 1
    assert ("PRICING#a.stale", "PUBLISHED") not in fake.items
    assert ("PRICING#a.kept", "PUBLISHED") in fake.items
    assert stats["stale"] == 1


def test_gc_targeted_diff_never_deletes_on_partial_refresh():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    fake.seed(_pub("a.stale"))
    mod._gc(written_keys={"a.kept"}, current_unmatched=set(), full_success=False,
            prior_keys=["a.kept", "a.stale"], prior_unmatched=[])
    assert ("PRICING#a.stale", "PUBLISHED") in fake.items  # survives partial


def test_read_prior_meta_parses_model_keys_and_unmatched():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    fake.seed({
        "pk": {"S": "PRICING#_CATALOG"}, "sk": {"S": "META"},
        "refresh_generation": {"N": "7"},
        "model_keys": {"L": [{"S": "a.one"}, {"S": "a.two"}]},
        "unmatched_names": {"L": [{"S": "Legacy Name"}]},
    })
    gen, keys, un = mod._read_prior_meta()
    assert gen == 7 and keys == ["a.one", "a.two"] and un == ["Legacy Name"]


def test_read_prior_meta_none_lists_when_pre_d9_meta():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    fake.seed({"pk": {"S": "PRICING#_CATALOG"}, "sk": {"S": "META"},
               "refresh_generation": {"N": "3"}})
    gen, keys, un = mod._read_prior_meta()
    assert gen == 3 and keys is None and un is None


# ── _priced(): resolver bridge over the refresher's in-memory grid ───────────

def test_priced_true_when_input_and_output_resolve():
    mod = _load_refresher()
    grid = {"in_region": {"standard": {"default": {"input": 5.0, "output": 10.0}}}}
    resolved = {"a.m1": {"grid": grid, "extra_ids": set()}}
    keys_by_canonical = {"a.m1": {"a.m1"}}
    priced, source = mod._priced(resolved, keys_by_canonical, "a.m1")
    assert priced and source == "aws-published"


def test_priced_false_when_no_grid():
    mod = _load_refresher()
    priced, source = mod._priced({}, {}, "a.absent")
    assert not priced and source is None


def test_priced_resolves_via_alias_key():
    mod = _load_refresher()
    grid = {"in_region": {"standard": {"default": {"input": 1.0, "output": 2.0}}}}
    resolved = {"a.canon": {"grid": grid, "extra_ids": set()}}
    keys_by_canonical = {"a.canon": {"a.canon", "a.canon-20250101"}}
    priced, _ = mod._priced(resolved, keys_by_canonical, "a.canon-20250101")
    assert priced


# ── coverage item write shape (contract §2) ──────────────────────────────────

def test_write_coverage_item_shape():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    coverage = {
        "counts": {"invokable": 2, "invokable_priced": 1,
                   "invokable_unpriced": 1, "listed_not_available": 0},
        "models": [
            {"id": "a.m1", "lanes": ["chat_completions"], "listed": True,
             "catalog_available": True, "priced": True, "source": "aws-published",
             "reason": "ok"},
            {"id": "a.m2", "lanes": [], "listed": False, "catalog_available": True,
             "priced": False, "source": None, "reason": "no-pricing-row"},
        ],
        "catalog_error": None,
    }
    mod._write_coverage(coverage, generation=9, now=1234, region="us-east-1")
    item = fake.items[("PRICING#_COVERAGE", "META")]
    assert item["refresh_generation"]["N"] == "9"
    assert item["computed_at"]["N"] == "1234"
    assert item["region"]["S"] == "us-east-1"
    assert item["counts"]["M"]["invokable_unpriced"]["N"] == "1"
    m2 = item["models"]["L"][1]["M"]
    assert m2["id"]["S"] == "a.m2"
    assert m2["source"] == {"NULL": True}
    assert m2["priced"]["BOOL"] is False
    assert "error" not in item  # no catalog_error ⇒ no error field


def test_write_coverage_records_catalog_error():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    mod._write_coverage(
        {"counts": {"invokable": 0, "invokable_priced": 0, "invokable_unpriced": 0,
                    "listed_not_available": 0},
         "models": [], "catalog_error": "TimeoutError: x"},
        generation=1, now=1, region="us-east-1")
    item = fake.items[("PRICING#_COVERAGE", "META")]
    assert item["error"]["S"] == "TimeoutError: x"
