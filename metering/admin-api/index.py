# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering admin API — the operator control surface (design §4.5), outside Open WebUI.

Routes (API Gateway HTTP API, Cognito JWT authorizer in front; this Lambda
additionally requires membership in one of ADMIN_GROUPS for every route
except GET /usage/me):

  GET  /policy/{scope}         scope ∈ DEFAULT | GROUP#<name> | USER#<sub>
  PUT  /policy/{scope}         body: {hard_limit_usd, soft_limit_usd, rpm_limit?, note?}
  GET  /usage/{sub}?window=    counter + open-estimate view for a subject
  GET  /usage/me               the caller's own counter (any authenticated user)
  POST /override               body: {sub, hard_limit_usd, until? (epoch)}
  POST /counter-reset          body: {sub, window?}   (self-target REJECTED)

Every mutation writes an AUDIT# row (actor, action, target, before, after) —
the usage ledger is never the audit trail (design §4.3). Admins cannot reset
or override their own counters; a second admin must act (security review #4).
"""

import datetime
import json
import logging
import os
import time

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
ADMIN_GROUPS = set(json.loads(os.environ.get("ADMIN_GROUPS", '["admins", "webui-admins", "admin"]')))

ddb = boto3.client("dynamodb")


def _resp(status: int, body: dict):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _claims(event) -> dict:
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims", {}) or {}


def _groups(claims: dict) -> list:
    g = claims.get("cognito:groups") or ""
    if isinstance(g, str):
        # HTTP API renders list claims as "[a b]"
        g = [x for x in g.strip("[]").replace(",", " ").split() if x]
    return list(g)


def _is_admin(claims: dict) -> bool:
    return bool(ADMIN_GROUPS.intersection(_groups(claims)))


def _audit(actor: str, action: str, target: str, before: dict, after: dict):
    now = int(time.time())
    day = datetime.datetime.utcfromtimestamp(now).strftime("%Y-%m-%d")
    ddb.put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": f"AUDIT#{day}"},
            "sk": {"S": f"{now}#{actor}#{action}"},
            "actor": {"S": actor},
            "action": {"S": action},
            "target": {"S": target},
            "before": {"S": json.dumps(before)[:4000]},
            "after": {"S": json.dumps(after)[:4000]},
            "ttl": {"N": str(now + 15 * 30 * 24 * 3600)},
        },
    )


def _plain(item: dict) -> dict:
    out = {}
    for k, v in (item or {}).items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            out[k] = float(v["N"])
    return out


def _get_policy(scope: str) -> dict:
    item = ddb.get_item(
        TableName=TABLE, Key={"pk": {"S": f"POLICY#{scope}"}, "sk": {"S": "POLICY"}}
    ).get("Item")
    return _plain(item)


def _put_policy(scope: str, body: dict, actor: str) -> dict:
    before = _get_policy(scope)
    item = {
        "pk": {"S": f"POLICY#{scope}"},
        "sk": {"S": "POLICY"},
        "hard_limit_usd": {"N": str(float(body["hard_limit_usd"]))},
        "soft_limit_usd": {"N": str(float(body.get("soft_limit_usd", body["hard_limit_usd"])))},
        "rpm_limit": {"N": str(int(body.get("rpm_limit", 30)))},
        "updated_by": {"S": actor},
        "updated_at": {"N": str(int(time.time()))},
    }
    if body.get("note"):
        item["note"] = {"S": str(body["note"])[:500]}
    if body.get("until"):
        item["override_until"] = {"N": str(int(body["until"]))}
    ddb.put_item(TableName=TABLE, Item=item)
    after = _get_policy(scope)
    _audit(actor, "PUT_POLICY", scope, before, after)
    return after


def _usage(sub: str, window: str) -> dict:
    counter = _plain(
        ddb.get_item(
            TableName=TABLE, Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}}
        ).get("Item")
    )
    policy = _get_policy(f"USER#{sub}") or _get_policy("DEFAULT")
    return {"sub": sub, "window": window, "counter": counter, "policy": policy}


def handler(event, context):
    claims = _claims(event)
    actor = claims.get("sub", "unknown")
    route = event.get("routeKey", "")
    path_params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    window = qs.get("window") or time.strftime("%Y-%m", time.gmtime())

    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return _resp(400, {"error": "invalid JSON body"})

    # self-service usage view for any authenticated user
    if route == "GET /usage/me":
        return _resp(200, _usage(actor, window))

    if not _is_admin(claims):
        return _resp(403, {"error": "admin group membership required"})

    if route == "GET /policy/{scope}":
        return _resp(200, _get_policy(path_params.get("scope", "DEFAULT")) or {})

    if route == "PUT /policy/{scope}":
        scope = path_params.get("scope", "")
        if not scope or "hard_limit_usd" not in body:
            return _resp(400, {"error": "scope path param and hard_limit_usd required"})
        if scope == f"USER#{actor}":
            return _resp(403, {"error": "self-targeted policy changes are rejected; a second admin must act"})
        return _resp(200, _put_policy(scope, body, actor))

    if route == "GET /usage/{sub}":
        return _resp(200, _usage(path_params.get("sub", ""), window))

    if route == "POST /override":
        sub = body.get("sub", "")
        if not sub or "hard_limit_usd" not in body:
            return _resp(400, {"error": "sub and hard_limit_usd required"})
        if sub == actor:
            return _resp(403, {"error": "self-targeted overrides are rejected; a second admin must act"})
        return _resp(200, _put_policy(f"USER#{sub}", body, actor))

    if route == "POST /counter-reset":
        sub = body.get("sub", "")
        if not sub:
            return _resp(400, {"error": "sub required"})
        if sub == actor:
            return _resp(403, {"error": "self-targeted resets are rejected; a second admin must act"})
        w = body.get("window") or window
        key = {"pk": {"S": f"USE#{sub}#{w}"}, "sk": {"S": "COUNTER"}}
        before = _plain(ddb.get_item(TableName=TABLE, Key=key).get("Item"))
        ddb.update_item(
            TableName=TABLE,
            Key=key,
            UpdateExpression="SET used_usd = :z, est_usd = :z, alerted = :none, updated_at = :now",
            ExpressionAttributeValues={
                ":z": {"N": "0"},
                ":none": {"S": ""},
                ":now": {"N": str(int(time.time()))},
            },
        )
        _audit(actor, "COUNTER_RESET", f"{sub}#{w}", before, {"used_usd": 0, "est_usd": 0})
        return _resp(200, {"reset": True, "sub": sub, "window": w})

    return _resp(404, {"error": f"unknown route {route}"})
