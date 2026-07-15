# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering sweeper Lambda — resolves orphaned admission estimates.

Runs on a schedule (every 5 minutes). Any interceptor estimate still OPEN
after MAX_STREAM_SECONDS (default 900 = 15 min) can no longer settle — the
stream died client-side, the usage event was lost, or the caller never
produced one (direct-to-gateway callers with no capture point).

Default resolution: REFUND — subtract the estimate from the counter and mark
the estimate REFUNDED (design §4.2: a lost usage event must not permanently
consume quota; the refunded volume is itself the abort/direct-caller
measurement). Strict mode (SWEEPER_MODE=settle) converts the estimate to a
settled ledger row instead — spend-conservative deployments over-charge
rather than under-count.

The scan uses the ESTIMATES GSI (state = OPEN) and is fine at sample scale;
the OPEN set is bounded by max-stream-duration × request rate.
"""

import datetime
import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
MAX_STREAM_SECONDS = int(os.environ.get("MAX_STREAM_SECONDS", "900"))
MODE = os.environ.get("SWEEPER_MODE", "refund")  # refund | settle

ddb = boto3.client("dynamodb")
cw = boto3.client("cloudwatch")


def _metric(name: str, value: float, unit: str = "Count"):
    try:
        cw.put_metric_data(
            Namespace="Metering",
            MetricData=[{"MetricName": name, "Value": value, "Unit": unit}],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"metric {name} failed: {e}")


def _resolve(est: dict, now: int):
    key = est["pk"]["S"]  # EST#<idem-key>
    sub = est.get("sub", {}).get("S", "unknown")
    window = est.get("window", {}).get("S", datetime.datetime.utcfromtimestamp(now).strftime("%Y-%m"))
    usd = float(est.get("usd", {}).get("N", "0"))
    counter_key = {"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}}

    if MODE == "settle":
        day = datetime.datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
        tx = [
            {
                "Update": {
                    "TableName": TABLE,
                    "Key": {"pk": {"S": key}, "sk": {"S": "EST"}},
                    "UpdateExpression": "SET #s = :swept",
                    "ConditionExpression": "#s = :open",
                    "ExpressionAttributeNames": {"#s": "state"},
                    "ExpressionAttributeValues": {":swept": {"S": "SETTLED_AT_ESTIMATE"}, ":open": {"S": "OPEN"}},
                }
            },
            {
                "Update": {
                    "TableName": TABLE,
                    "Key": counter_key,
                    # move the estimate into used_usd (net total unchanged)
                    "UpdateExpression": "ADD used_usd :u, est_usd :neg SET updated_at = :now",
                    "ExpressionAttributeValues": {
                        ":u": {"N": str(usd)},
                        ":neg": {"N": str(-usd)},
                        ":now": {"N": str(now)},
                    },
                }
            },
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": {
                        "pk": {"S": f"LEDGER#{day}"},
                        "sk": {"S": f"{now}#{key}"},
                        "idem": {"S": key.removeprefix('EST#')},
                        "sub": {"S": sub},
                        "billing_group": est.get("billing_group", {"S": "unassigned"}),
                        "model": est.get("model", {"S": "unknown"}),
                        "lane": est.get("lane", {"S": "unknown"}),
                        "tier": {"S": "standard"},
                        "tokens_in": est.get("tokens_in", {"N": "0"}),
                        "tokens_out": est.get("tokens_out", {"N": "0"}),
                        "usd": {"N": str(usd)},
                        "state": {"S": "SETTLED_AT_ESTIMATE"},
                        "source": {"S": "SWEEPER"},
                        "ttl": {"N": str(now + 15 * 30 * 24 * 3600)},
                    },
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
    else:  # refund
        tx = [
            {
                "Update": {
                    "TableName": TABLE,
                    "Key": {"pk": {"S": key}, "sk": {"S": "EST"}},
                    "UpdateExpression": "SET #s = :refunded",
                    "ConditionExpression": "#s = :open",
                    "ExpressionAttributeNames": {"#s": "state"},
                    "ExpressionAttributeValues": {":refunded": {"S": "REFUNDED"}, ":open": {"S": "OPEN"}},
                }
            },
            {
                "Update": {
                    "TableName": TABLE,
                    "Key": counter_key,
                    "UpdateExpression": "ADD est_usd :neg SET updated_at = :now",
                    "ExpressionAttributeValues": {":neg": {"N": str(-usd)}, ":now": {"N": str(now)}},
                }
            },
        ]
    try:
        ddb.transact_write_items(TransactItems=tx)
        return usd
    except ClientError as e:
        if "TransactionCanceled" in str(e):
            # a settle raced us and won — correct outcome, nothing to do
            log.info(f"estimate {key} settled concurrently; skipped")
            return 0.0
        raise


def handler(event, context):
    now = int(time.time())
    cutoff = now - MAX_STREAM_SECONDS
    resolved, resolved_usd = 0, 0.0
    kwargs = {
        "TableName": TABLE,
        "IndexName": "estimates",
        "KeyConditionExpression": "#s = :open AND created_at < :cutoff",
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": {":open": {"S": "OPEN"}, ":cutoff": {"N": str(cutoff)}},
        "Limit": 200,
    }
    while True:
        page = ddb.query(**kwargs)
        for est in page.get("Items", []):
            resolved_usd += _resolve(est, now)
            resolved += 1
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    if resolved:
        _metric("SweptEstimates", resolved)
        _metric("SweptEstimateUSD", resolved_usd, "None")
    log.info(json.dumps({"resolved": resolved, "usd": round(resolved_usd, 6), "mode": MODE}))
