# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering reconciler — nightly ledger-vs-invoice drift check (design M3).

Runs at ~06:00 UTC comparing day D-2 (Cost Explorer needs ~24h; D-2 avoids
false drift on slow days):

  metered = Σ settled ledger tokens by (model, direction) for day D
  billed  = CE GetCostAndUsage(DAILY, USAGE_TYPE) mantle usage types × 1000
            (unit live-confirmed: 1K tokens/unit) — query includes both
            "Amazon Bedrock" and any Anthropic legal-entity service names,
            because Claude-on-Bedrock can invoice under the marketplace entity.

Publishes Metering/ReconciliationDriftPct per model and an aggregate; the
alarm thresholds live in the stack (page >5%, floor 100K tokens/day — below
the floor drift is statistically meaningless; a single aborted stream shows
as double-digit %). The unsettled-estimate baseline (aborts, direct callers,
OWUI-internal calls) is reported separately, not mixed into drift.
"""

import datetime
import json
import logging
import os
import re

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
REGION = os.environ.get("MANTLE_REGION", os.environ.get("AWS_REGION", "us-east-1"))
FLOOR_TOKENS = int(os.environ.get("FLOOR_TOKENS", "100000"))

ddb = boto3.client("dynamodb")
ce = boto3.client("ce", region_name="us-east-1")
cw = boto3.client("cloudwatch")

USAGE_RE = re.compile(r"^[A-Z0-9]+-(?P<model>.+)-mantle-(?P<direction>input|output)-tokens-(?P<tier>[a-z]+)$")


def _ledger_sums(day: str) -> dict:
    sums: dict = {}
    kwargs = {
        "TableName": TABLE,
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"LEDGER#{day}"}},
    }
    unsettled_usd = 0.0
    while True:
        page = ddb.query(**kwargs)
        for it in page.get("Items", []):
            state = it.get("state", {}).get("S", "")
            model = it.get("model", {}).get("S", "unknown")
            if state in ("SETTLED", "SETTLED_AT_ESTIMATE"):
                agg = sums.setdefault(model, {"input": 0, "output": 0})
                agg["input"] += int(it.get("tokens_in", {}).get("N", "0"))
                agg["output"] += int(it.get("tokens_out", {}).get("N", "0"))
            else:
                unsettled_usd += float(it.get("usd", {}).get("N", "0"))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return {"models": sums, "unsettled_usd": unsettled_usd}


def _billed_sums(day: str) -> dict:
    end = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": day, "End": end},
        Granularity="DAILY",
        Metrics=["UsageQuantity"],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        # No SERVICE filter: Anthropic-entity line items must not silently
        # exit the comparison (FinOps review #1). The usage-type regex is the
        # mantle selector.
    )
    billed: dict = {}
    for r in resp.get("ResultsByTime", []):
        for g in r.get("Groups", []):
            m = USAGE_RE.match(g["Keys"][0])
            if not m:
                continue
            tokens = int(float(g["Metrics"]["UsageQuantity"]["Amount"]) * 1000)
            agg = billed.setdefault(m["model"], {"input": 0, "output": 0})
            agg[m["direction"]] += tokens
    return billed


def _metric(name: str, value: float, dims: dict | None = None, unit: str = "None"):
    cw.put_metric_data(
        Namespace="Metering",
        MetricData=[{
            "MetricName": name, "Value": value, "Unit": unit,
            "Dimensions": [{"Name": k, "Value": v} for k, v in (dims or {}).items()],
        }],
    )


def handler(event, context):
    day = (datetime.datetime.utcnow().date() - datetime.timedelta(days=2)).isoformat()
    ledger = _ledger_sums(day)
    billed = _billed_sums(day)

    report = {"day": day, "models": {}, "unsettled_usd": round(ledger["unsettled_usd"], 6)}
    total_metered, total_billed = 0, 0
    for model in sorted(set(ledger["models"]) | set(billed)):
        m = ledger["models"].get(model, {"input": 0, "output": 0})
        b = billed.get(model, {"input": 0, "output": 0})
        mt, bt = m["input"] + m["output"], b["input"] + b["output"]
        total_metered += mt
        total_billed += bt
        drift = None if bt == 0 else round(100.0 * (bt - mt) / bt, 2)
        report["models"][model] = {"metered": mt, "billed": bt, "drift_pct": drift}
        if bt >= FLOOR_TOKENS and drift is not None:
            _metric("ReconciliationDriftPct", abs(drift), {"Model": model[:64]}, "Percent")

    agg_drift = None if total_billed == 0 else round(100.0 * (total_billed - total_metered) / total_billed, 2)
    report["total"] = {"metered": total_metered, "billed": total_billed, "drift_pct": agg_drift}
    if total_billed >= FLOOR_TOKENS and agg_drift is not None:
        _metric("ReconciliationDriftPct", abs(agg_drift), {"Model": "ALL"}, "Percent")
    _metric("UnsettledEstimateUSD", report["unsettled_usd"])
    log.info(json.dumps(report))
    return report
