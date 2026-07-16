# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering debit Lambda — settles usage events against the quota counter.

Triggered by the metering EventBridge bus (detail-type "usage", emitted by the
seeded Open WebUI filter and, for estimates, by the gateway interceptor).

Settlement contract (design §4.2 E2):
  TransactWriteItems {
    Put ledger row     IF attribute_not_exists(pk)   ← first-writer-wins
    Update user counter ADD used_usd/actual tokens, subtract the matching estimate
  }
The transaction makes the idempotency guard and the counter increment atomic:
a crash between them cannot drop or double-apply a debit. Group rollups are
NOT in the transaction (hot-partition risk) — the rollup Lambda builds them
from this table's DynamoDB Stream.

Idempotency key preference (design §4.1): provider response id →
(chat_id, message_id) → caller-supplied estimate key.

Environment: TABLE (single-table name), PRICE_MAP (JSON string; from
config/model-prices.json), SNS_TOPIC (threshold alerts), SOFT_DEFAULT/HARD_DEFAULT.
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


def _load_price_map() -> dict:
    """Env override for tests; bundled model-prices.json in the deploy asset
    (the full map is ~17 KB — over Lambda's 4 KB env ceiling)."""
    if os.environ.get("PRICE_MAP"):
        return json.loads(os.environ["PRICE_MAP"])
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "model-prices.json")) as f:
            raw = json.load(f)
        return {"version": raw.get("version"), "models": {**raw.get("models", {}), **raw.get("overrides", {})}}
    except (OSError, ValueError):
        return {}


PRICE_MAP = _load_price_map()
PRICE_MAP_VERSION = PRICE_MAP.get("version", "unversioned")
SNS_TOPIC = os.environ.get("SNS_TOPIC", "")
CANARY_SUBS = set(filter(None, os.environ.get("CANARY_SUBS", "").split(",")))
# Same precedence list the interceptor uses (config/metering-groups.json order)
# so estimate rows and settled ledger rows agree on billing_group.
GROUP_ORDER = json.loads(os.environ.get("GROUP_ORDER", "[]"))


def _billing_group(groups: list) -> str:
    for configured in GROUP_ORDER:
        if configured in groups:
            return configured
    return "unassigned"

ddb = boto3.client("dynamodb")
sns = boto3.client("sns") if SNS_TOPIC else None
cw = boto3.client("cloudwatch")


def _month_window(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m")


def _rate(model: str, direction: str, tier: str = "standard"):
    """Return $/token for (model, direction, tier), or None when unpriced.

    Unpriced models are a blocking onboarding state (design M3): the debit
    still records TOKENS (the invariant), prices at 0, and emits an
    UnpricedModel metric so operators enter a rate rather than us guessing.
    """
    entry = (PRICE_MAP.get("models") or {}).get(model)
    if not entry:
        return None
    v = (entry.get(tier) or entry.get("standard") or {}).get(direction)
    return float(v) if v is not None else None


def _idempotency_key(d: dict) -> str:
    if d.get("response_id"):
        return f"resp#{d['response_id']}"
    if d.get("chat_id") and d.get("message_id"):
        return f"msg#{d['chat_id']}#{d['message_id']}"
    if d.get("estimate_key"):
        return f"est#{d['estimate_key']}"
    return f"ts#{d.get('sub','?')}#{d.get('ts', int(time.time()))}"


def _metric(name: str, value: float = 1, unit: str = "Count", dims: dict | None = None):
    try:
        cw.put_metric_data(
            Namespace="Metering",
            MetricData=[{
                "MetricName": name,
                "Value": value,
                "Unit": unit,
                "Dimensions": [{"Name": k, "Value": v} for k, v in (dims or {}).items()],
            }],
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"metric {name} failed: {e}")


def _settle(detail: dict):
    sub = detail.get("sub", "unknown")
    ts = int(detail.get("ts", time.time()))
    window = _month_window(ts)
    model = detail.get("model", "unknown")
    # model ids arrive gateway-qualified ("bedrock/…") or pipe-prefixed ("metering.…") — normalize
    model = model.split("/", 1)[-1]
    if "." in model and model.split(".", 1)[0] in ("gateway_anthropic", "metering"):
        model = model.split(".", 1)[1]
    tier = detail.get("tier", "standard")
    tin = int(detail.get("input_tokens", 0))
    tout = int(detail.get("output_tokens", 0))
    tcached = int(detail.get("cached_tokens", 0))

    rin, rout = _rate(model, "input", tier), _rate(model, "output", tier)
    unpriced = rin is None or rout is None
    usd = 0.0 if unpriced else round(tin * rin + tout * rout, 8)
    if unpriced:
        _metric("UnpricedModel", dims={"Model": model[:64]})

    key = _idempotency_key(detail)
    day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
    billing_group = _billing_group(detail.get("groups") or [])

    ledger_item = {
        "pk": {"S": f"LEDGER#{day}"},
        "sk": {"S": f"{ts}#{key}"},
        "idem": {"S": key},
        "sub": {"S": sub},
        "billing_group": {"S": billing_group},
        "groups": {"SS": list(set((detail.get("groups") or ["unassigned"])))},
        "model": {"S": model},
        "lane": {"S": detail.get("lane", "unknown")},
        "tier": {"S": tier},
        "tokens_in": {"N": str(tin)},
        "tokens_out": {"N": str(tout)},
        "tokens_cached": {"N": str(tcached)},
        "rate_in": {"N": str(rin or 0)},
        "rate_out": {"N": str(rout or 0)},
        "price_map_version": {"S": PRICE_MAP_VERSION},
        "usd": {"N": str(usd)},
        "state": {"S": "SETTLED"},
        "source": {"S": detail.get("source", "FILTER")},
        "ttl": {"N": str(ts + 15 * 30 * 24 * 3600)},  # ~15 months
    }
    # A GSI on idem lets the sweeper and admin API look up by key.
    counter_key = {"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}}

    # Settle consumes the caller's OPEN admission estimate. The interceptor's
    # estimate key is a gateway-side hash (sub + body + minute) the app-tier
    # capture can never reconstruct, so matching is by CONTENT, not key: the
    # oldest OPEN estimate for the same (sub, model). The OPEN set is small by
    # construction (bounded by max-stream-duration × request rate), so a
    # filtered GSI query is cheap.
    est_usd = 0.0
    est_used = False
    est_pk = None
    try:
        page = ddb.query(
            TableName=TABLE,
            IndexName="estimates",
            KeyConditionExpression="#s = :open",
            FilterExpression="#sub = :sub AND #m = :model",
            ExpressionAttributeNames={"#s": "state", "#sub": "sub", "#m": "model"},
            ExpressionAttributeValues={
                ":open": {"S": "OPEN"},
                ":sub": {"S": sub},
                ":model": {"S": model},
            },
            ScanIndexForward=True,  # oldest first
            Limit=50,
        )
        items = page.get("Items", [])
        if items:
            est = items[0]
            est_pk = est["pk"]["S"]
            est_usd = float(est.get("usd", {}).get("N", "0"))
            est_used = True
    except ClientError as e:
        log.warning(f"estimate lookup failed for {key}: {e}")

    # Counter model: total-in-force = used_usd + est_usd. Settlement moves the
    # matched estimate out of est_usd and the actual into used_usd. The w stamp
    # projects the counter into the by-window GSI (admin console reads, D4).
    update_expr = "ADD used_usd :d, used_in :i, used_out :o, req_count :one SET updated_at = :now, w = :w"
    values = {
        ":d": {"N": str(round(usd, 8))},
        ":i": {"N": str(tin)},
        ":o": {"N": str(tout)},
        ":one": {"N": "1"},
        ":now": {"N": str(ts)},
        ":w": {"S": window},
    }
    if est_used:
        update_expr += ", est_usd = if_not_exists(est_usd, :zero) - :e"
        values[":e"] = {"N": str(est_usd)}
        values[":zero"] = {"N": "0"}
    tx = [
        {
            "Put": {
                "TableName": TABLE,
                "Item": ledger_item,
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        },
        {
            "Update": {
                "TableName": TABLE,
                "Key": counter_key,
                "UpdateExpression": update_expr,
                "ExpressionAttributeValues": values,
            }
        },
    ]
    if est_used and est_pk:
        tx.append(
            {
                "Update": {
                    "TableName": TABLE,
                    "Key": {"pk": {"S": est_pk}, "sk": {"S": "EST"}},
                    "UpdateExpression": "SET #s = :settled",
                    "ConditionExpression": "#s = :open",
                    "ExpressionAttributeNames": {"#s": "state"},
                    "ExpressionAttributeValues": {
                        ":settled": {"S": "SETTLED"},
                        ":open": {"S": "OPEN"},
                    },
                }
            }
        )
    try:
        ddb.transact_write_items(TransactItems=tx)
    except ClientError as e:
        if "TransactionCanceled" not in str(e) and "ConditionalCheckFailed" not in str(e):
            raise
        # Two distinct cancellation causes:
        #  (a) ledger row exists → duplicate event (EventBridge at-least-once) — done;
        #  (b) the matched estimate was concurrently swept/settled → retry once
        #      WITHOUT the estimate branch (the actual still must land).
        dup = ddb.get_item(
            TableName=TABLE,
            Key={"pk": ledger_item["pk"], "sk": ledger_item["sk"]},
            ConsistentRead=True,
        ).get("Item")
        if dup:
            log.info(f"duplicate settle skipped for {key}")
            _metric("DuplicateSettles")
            return
        log.info(f"estimate raced for {key}; settling without estimate consumption")
        ddb.transact_write_items(TransactItems=tx[:2] if not est_used else [
            tx[0],
            {
                "Update": {
                    "TableName": TABLE,
                    "Key": counter_key,
                    "UpdateExpression": "ADD used_usd :d, used_in :i, used_out :o, req_count :one SET updated_at = :now, w = :w",
                    "ExpressionAttributeValues": {
                        ":d": {"N": str(round(usd, 8))},
                        ":i": {"N": str(tin)},
                        ":o": {"N": str(tout)},
                        ":one": {"N": "1"},
                        ":now": {"N": str(ts)},
                        ":w": {"S": window},
                    },
                }
            },
        ])

    if sub not in CANARY_SUBS:
        _metric("SettledUSD", usd, "None")
        _metric("SettledCalls")
    _threshold_check(sub, window)


def _resolve_limits(sub: str) -> tuple[float, float]:
    """Policy precedence: USER# override → DEFAULT → env defaults."""
    for scope in (f"USER#{sub}", "DEFAULT"):
        item = ddb.get_item(
            TableName=TABLE, Key={"pk": {"S": f"POLICY#{scope}"}, "sk": {"S": "POLICY"}}
        ).get("Item")
        if item:
            hard = float(item.get("hard_limit_usd", {}).get("N", "0"))
            soft = float(item.get("soft_limit_usd", {}).get("N", str(hard * 0.8)))
            return hard, soft
    return float(os.environ.get("HARD_DEFAULT_USD", "5")), float(os.environ.get("SOFT_DEFAULT_USD", "4"))


def _threshold_check(sub: str, window: str):
    """Post-settle: stamp resolved limits onto the counter (the inlet's
    single-GetItem soft-warn snapshot reads them there) + 80/100% alerts."""
    try:
        hard, soft = _resolve_limits(sub)
        item = ddb.get_item(
            TableName=TABLE,
            Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
        ).get("Item") or {}
        used = float(item.get("used_usd", {}).get("N", "0")) + max(0.0, float(item.get("est_usd", {}).get("N", "0")))
        stamped_hard = float(item.get("hard_limit_usd", {}).get("N", "-1"))
        already = item.get("alerted", {}).get("S", "")
        level = "" if hard <= 0 else ("100" if used >= hard else ("80" if used >= 0.8 * hard else ""))

        if abs(stamped_hard - hard) > 1e-9 or (level and level != already):
            expr = "SET hard_limit_usd = :h, soft_limit_usd = :s"
            vals = {":h": {"N": str(hard)}, ":s": {"N": str(soft)}}
            if level and level != already:
                expr += ", alerted = :l"
                vals[":l"] = {"S": level}
            ddb.update_item(
                TableName=TABLE,
                Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
                UpdateExpression=expr,
                ExpressionAttributeValues=vals,
            )
        if sns and level and level != already:
            sns.publish(
                TopicArn=SNS_TOPIC,
                Subject=f"Metering: user at {level}% of quota",
                Message=json.dumps({"sub": sub, "window": window, "used_usd": used, "hard_limit_usd": hard}),
            )
    except Exception as e:  # noqa: BLE001
        log.warning(f"threshold check failed for {sub}: {e}")


def handler(event, context):
    detail = event.get("detail") or {}
    if not detail:
        log.warning("no detail on event; skipping")
        return
    _settle(detail)
