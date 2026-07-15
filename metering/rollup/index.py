# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering rollup Lambda — builds group counters from the table's DynamoDB Stream.

Group rollups are deliberately OUTSIDE the settle transaction (design §4.2 E2):
a hot GROUP# item inside TransactWriteItems would serialize every settle in a
large team onto one partition key at peak. Ceiling checks tolerate seconds of
lag, so rollups build asynchronously here instead.

Input: INSERT events for LEDGER# rows (stream filtered by the CDK event source).
Effect: ADD usd/tokens to GROUP#<billing_group>#<window> counters.
Idempotency: stream delivery is at-least-once per shard iterator; batch items
are deduped within an invoke, and re-delivery after a partial failure is rare
enough that group ceilings (soft, advisory relative to per-user hard limits)
accept the drift. Exact chargeback always comes from the ledger, never these
counters.
"""

import datetime
import logging
import os
import time

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
ddb = boto3.client("dynamodb")


def handler(event, context):
    seen: set[str] = set()
    adds: dict[tuple[str, str], dict] = {}
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        img = (rec.get("dynamodb") or {}).get("NewImage") or {}
        pk = img.get("pk", {}).get("S", "")
        if not pk.startswith("LEDGER#"):
            continue
        idem = img.get("idem", {}).get("S", "")
        if idem in seen:
            continue
        seen.add(idem)
        state = img.get("state", {}).get("S", "")
        if state not in ("SETTLED", "SETTLED_AT_ESTIMATE"):
            continue
        group = img.get("billing_group", {}).get("S", "unassigned")
        day = pk.removeprefix("LEDGER#")
        window = day[:7]
        key = (group, window)
        agg = adds.setdefault(key, {"usd": 0.0, "tin": 0, "tout": 0, "n": 0})
        agg["usd"] += float(img.get("usd", {}).get("N", "0"))
        agg["tin"] += int(img.get("tokens_in", {}).get("N", "0"))
        agg["tout"] += int(img.get("tokens_out", {}).get("N", "0"))
        agg["n"] += 1

    for (group, window), agg in adds.items():
        ddb.update_item(
            TableName=TABLE,
            Key={"pk": {"S": f"GROUP#{group}#{window}"}, "sk": {"S": "COUNTER"}},
            UpdateExpression="ADD used_usd :u, used_in :i, used_out :o, req_count :n SET updated_at = :now",
            ExpressionAttributeValues={
                ":u": {"N": str(round(agg["usd"], 8))},
                ":i": {"N": str(agg["tin"])},
                ":o": {"N": str(agg["tout"])},
                ":n": {"N": str(agg["n"])},
                ":now": {"N": str(int(time.time()))},
            },
        )
    if adds:
        log.info(f"rolled up {len(seen)} ledger rows into {len(adds)} group counters")
