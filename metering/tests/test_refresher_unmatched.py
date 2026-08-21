# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Refresher D8 unmatched-classification tests (contract §4).

Each PRICING#_UNMATCHED row gains a 'class': no-match (zero control-plane
candidates — historical) | ambiguous (>1 candidates — actionable). Only
ambiguous entries drive PricingUnmatchedActionable.

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
        "pricing_refresher_um", HERE.parent / "pricing-refresher" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pricing_refresher_um"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeDdb:
    def __init__(self):
        self.items = {}

    def put_item(self, TableName=None, Item=None, **kw):
        self.items[(Item["pk"]["S"], Item["sk"]["S"])] = Item
        return {}


def test_write_unmatched_persists_class_field():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    unmatched = {
        "No Twin Product": {"provider": "x", "service_code": "AmazonBedrock",
                            "reason": mod.REASON_NO_MATCH, "class": mod.CLASS_NO_MATCH,
                            "grid": {}},
        "Two Candidates": {"provider": "y", "service_code": "AmazonBedrock",
                           "reason": mod.REASON_AMBIGUOUS, "class": mod.CLASS_AMBIGUOUS,
                           "grid": {}},
    }
    mod._write_unmatched(unmatched, generation=1, now=1)
    nm = fake.items[("PRICING#_UNMATCHED", "No Twin Product")]
    am = fake.items[("PRICING#_UNMATCHED", "Two Candidates")]
    assert nm["class"]["S"] == "no-match"
    assert am["class"]["S"] == "ambiguous"


def test_write_unmatched_defaults_class_to_no_match():
    mod = _load_refresher()
    fake = FakeDdb()
    mod.ddb = fake
    # a legacy dict missing 'class' reads as historical (no-match)
    mod._write_unmatched({"Legacy": {"grid": {}}}, generation=1, now=1)
    assert fake.items[("PRICING#_UNMATCHED", "Legacy")]["class"]["S"] == "no-match"


def test_actionable_metric_counts_only_ambiguous():
    mod = _load_refresher()
    emitted = {}
    mod._metric = lambda name, value=1, unit="Count": emitted.__setitem__(name, value)
    unmatched = {
        "hist1": {"class": mod.CLASS_NO_MATCH},
        "hist2": {"class": mod.CLASS_NO_MATCH},
        "act1": {"class": mod.CLASS_AMBIGUOUS},
    }
    coverage = {"counts": {"invokable_unpriced": 0}}
    mod._emit_coverage_metrics(coverage, unmatched, rate_conflicts=0, unclassified=0)
    assert emitted["PricingUnmatchedActionable"] == 1


def test_coverage_metrics_emit_all_gauges():
    mod = _load_refresher()
    emitted = {}
    mod._metric = lambda name, value=1, unit="Count": emitted.__setitem__(name, value)
    coverage = {"counts": {"invokable_unpriced": 3}}
    mod._emit_coverage_metrics(coverage, {}, rate_conflicts=2, unclassified=4)
    assert emitted["UnpricedGatewayModels"] == 3
    assert emitted["PricingUnmatchedActionable"] == 0
    assert emitted["PricingRateConflict"] == 2
    assert emitted["PricingDimensionUnclassified"] == 4
