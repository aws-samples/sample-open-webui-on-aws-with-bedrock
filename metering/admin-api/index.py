# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering admin API — the operator control surface (design §4.5), outside Open WebUI.

Serves both the CLI (scripts/set-quota.sh) and the Metering Admin Console
(console/ — docs/plans/metering-admin-console/01-DECISIONS.md). API Gateway
HTTP API with a Cognito JWT authorizer in front; this Lambda additionally
requires membership in one of ADMIN_GROUPS for every route except the
self-service ones marked [self] below.

Mutations (audited; self-target REJECTED — a second admin must act):
  PUT    /policy/{scope}        scope ∈ DEFAULT | GROUP#<name> | USER#<sub>
                                body: {hard_limit_usd, soft_limit_usd?, rpm_limit?,
                                       note?, until? (epoch — time-boxed override)}
  DELETE /policy/{scope}        remove a policy row (revert scope to inheritance)
  POST   /override              body: {sub, hard_limit_usd, until?, note?}
  POST   /counter-reset         body: {sub, window?}
  POST   /alert-subscriptions   body: {email}     (SNS confirmation email sent)
  DELETE /alert-subscriptions?arn=<subscriptionArn>
  PUT    /pricing/{model}       operator rate override, USD per 1M tokens
  DELETE /pricing/{model}       remove override (revert to published/unpriced)
  POST   /pricing/alias         bind a Price List name to a model id
  DELETE /pricing/alias/{name}  remove a binding
  POST   /pricing/refresh       run the pricing refresher now

Reads:
  GET /config                   [self] module info for the console shell
  GET /usage/me                 [self] caller's counter + resolved policy
  GET /user/me/ledger           [self] caller's recent settled calls
  GET /usage/{sub}?window=      counter + policy for a subject (legacy shape)
  GET /users?window=&limit=&cursor=&filter=   spend-ordered user counters
  GET /users/search?q=          Cognito email/sub search → identity + groups
  GET /user/{sub}?window=       full drill-in (counter, policy chain, profile,
                                open estimates, recent ledger)
  GET /user/{sub}/ledger?limit=&cursor=
  GET /groups?window=           group rollup counters + group policies
  GET /policies                 all explicit policy rows + implicit env default
  GET /audit?days=&actor=       audit trail, newest first
  GET /activity?limit=          recent settled ledger feed (today + yesterday)
  GET /estimates                open admission estimates, oldest first
  GET /metrics?range=           CloudWatch Metering/* series for the dashboards
  GET /alarms                   module alarm states
  GET /alert-subscriptions      SNS subscriptions on the alerts topic

Every mutation writes an AUDIT# row (actor, action, target, before, after) —
the usage ledger is never the audit trail (design §4.3). Admins cannot reset,
override, or delete policies for themselves; a second admin must act
(security review #4). Reads use the by-window / by-sub / estimates GSIs —
never a table Scan (console decision D4).
"""

import base64
import concurrent.futures
import datetime
import json
import logging
import os
import re
import sys
import time

import boto3

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..")):  # Lambda task root / repo tree
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pricing.identity import MODEL_ID_RE  # noqa: E402
from pricing.resolver import UNIT_PER_1M, resolve_rate, unwrap_item  # noqa: E402

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ["TABLE"]
ADMIN_GROUPS = set(json.loads(os.environ.get("ADMIN_GROUPS", '["admins", "webui-admins", "admin"]')))
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "open-webui-metering")
ENFORCE_MODE = os.environ.get("ENFORCE_MODE", "")
PRICING_REFRESHER_FN = os.environ.get("PRICING_REFRESHER_FN", "")
HARD_DEFAULT_USD = float(os.environ.get("HARD_DEFAULT_USD", "5"))
SOFT_DEFAULT_USD = float(os.environ.get("SOFT_DEFAULT_USD", "4"))
RPM_DEFAULT = int(os.environ.get("RPM_LIMIT_DEFAULT", "30"))

ddb = boto3.client("dynamodb")
_clients: dict = {}
# Container-scoped caches: hot-read stampede guard (D4) + sub→email lookups.
_read_cache: dict = {}
_email_cache: dict = {}
READ_CACHE_TTL = 15


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


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
        elif "SS" in v:
            out[k] = list(v["SS"])
        elif "BOOL" in v:
            out[k] = v["BOOL"]
    return out


def _cursor_out(lek: dict | None) -> str | None:
    if not lek:
        return None
    return base64.urlsafe_b64encode(json.dumps(lek).encode()).decode()


def _cursor_in(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError):
        return None


def _window_now() -> str:
    return time.strftime("%Y-%m", time.gmtime())


# \Z (not $) so a trailing newline can't slip a bogus window into a DDB key.
_WINDOW_RE = re.compile(r"^\d{4}-\d{2}\Z")


def _safe_window(qs: dict) -> str:
    w = qs.get("window") or _window_now()
    return w if _WINDOW_RE.match(w) else _window_now()


def _int_param(qs: dict, key: str, default: int, lo: int, hi: int) -> int:
    """Bounded int query-param. A non-numeric value falls back to the default
    instead of raising (an uncaught int() would 500 the request)."""
    try:
        v = int(qs.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _cached(key: str, fn):
    hit = _read_cache.get(key)
    if hit and time.time() - hit[0] < READ_CACHE_TTL:
        return hit[1]
    val = fn()
    _read_cache[key] = (time.time(), val)
    return val


# ── policies ────────────────────────────────────────────────────────────────

def _get_policy(scope: str, consistent: bool = False) -> dict:
    item = ddb.get_item(
        TableName=TABLE,
        Key={"pk": {"S": f"POLICY#{scope}"}, "sk": {"S": "POLICY"}},
        ConsistentRead=consistent,
    ).get("Item")
    return _plain(item)


def _put_policy(scope: str, body: dict, actor: str) -> dict:
    # Audit before/after must be strongly consistent — an eventually-consistent
    # read right after put_item can record stale values (review finding).
    before = _get_policy(scope, consistent=True)
    now = int(time.time())
    item = {
        "pk": {"S": f"POLICY#{scope}"},
        "sk": {"S": "POLICY"},
        "hard_limit_usd": {"N": str(float(body["hard_limit_usd"]))},
        "soft_limit_usd": {"N": str(float(body.get("soft_limit_usd", body["hard_limit_usd"])))},
        "rpm_limit": {"N": str(int(body.get("rpm_limit", 30)))},
        "updated_by": {"S": actor},
        "updated_at": {"N": str(now)},
        # estimates-GSI stamps: policies list as state=POLICY (console D4);
        # never "OPEN", so the sweeper's estimate query can't see them.
        "state": {"S": "POLICY"},
        "created_at": {"N": str(now)},
    }
    if body.get("note"):
        item["note"] = {"S": str(body["note"])[:500]}
    if body.get("until"):
        until = int(body["until"])
        if until <= now:
            raise ValueError("until must be a future epoch timestamp")
        item["override_until"] = {"N": str(until)}
    ddb.put_item(TableName=TABLE, Item=item)
    after = _get_policy(scope, consistent=True)
    _audit(actor, "PUT_POLICY", scope, before, after)
    return after


def _delete_policy(scope: str, actor: str) -> dict:
    before = _get_policy(scope, consistent=True)
    if not before:
        return {"deleted": False, "reason": "no explicit policy at this scope"}
    ddb.delete_item(TableName=TABLE, Key={"pk": {"S": f"POLICY#{scope}"}, "sk": {"S": "POLICY"}})
    _audit(actor, "DELETE_POLICY", scope, before, {})
    return {"deleted": True, "scope": scope}


def _list_policies() -> dict:
    """All explicit policy rows (estimates GSI, state=POLICY) + the implicit default."""
    items, lek = [], None
    while True:
        kwargs = {
            "TableName": TABLE,
            "IndexName": "estimates",
            "KeyConditionExpression": "#s = :p",
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": {":p": {"S": "POLICY"}},
            "Limit": 200,
        }
        if lek:
            kwargs["ExclusiveStartKey"] = lek
        page = ddb.query(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            break
    policies = []
    for it in items:
        p = _plain(it)
        p["scope"] = p.get("pk", "").removeprefix("POLICY#")
        p.pop("pk", None), p.pop("sk", None), p.pop("state", None)
        policies.append(p)
    # Rows written before the state=POLICY stamp existed miss the GSI; DEFAULT
    # is the load-bearing one, so fetch it directly and merge if absent.
    if not any(p["scope"] == "DEFAULT" for p in policies):
        legacy_default = _get_policy("DEFAULT")
        if legacy_default:
            legacy_default["scope"] = "DEFAULT"
            legacy_default.pop("pk", None), legacy_default.pop("sk", None)
            policies.append(legacy_default)
    return {
        "policies": sorted(policies, key=lambda p: p.get("updated_at", 0), reverse=True),
        "implicit_default": {
            "scope": "DEFAULT (environment)",
            "hard_limit_usd": HARD_DEFAULT_USD,
            "soft_limit_usd": SOFT_DEFAULT_USD,
            "rpm_limit": RPM_DEFAULT,
            "note": "applies when no explicit DEFAULT policy row exists",
        },
    }


def _resolve_policy_chain(sub: str) -> dict:
    """The user's effective policy, mirroring the ENFORCEMENT precedence exactly:
    USER#<sub> override → explicit DEFAULT → environment default. GROUP#
    policies are advisory team ceilings (docs/METERING.md) and are reported
    on the group, never applied per-user — the chain is honest about that."""
    user_p = _get_policy(f"USER#{sub}")
    default_p = _get_policy("DEFAULT")
    env_p = {"hard_limit_usd": HARD_DEFAULT_USD, "soft_limit_usd": SOFT_DEFAULT_USD, "rpm_limit": RPM_DEFAULT}
    if user_p:
        effective, source = user_p, f"USER#{sub}"
    elif default_p:
        effective, source = default_p, "DEFAULT"
    else:
        effective, source = env_p, "DEFAULT (environment)"
    return {
        "effective": {k: v for k, v in effective.items() if not k.startswith(("pk", "sk"))},
        "source": source,
        "chain": {
            "user_override": user_p or None,
            "default": default_p or None,
            "environment": env_p,
        },
    }


# ── usage / counters ────────────────────────────────────────────────────────

def _usage(sub: str, window: str) -> dict:
    counter = _plain(
        ddb.get_item(
            TableName=TABLE, Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}}
        ).get("Item")
    )
    policy = _get_policy(f"USER#{sub}") or _get_policy("DEFAULT")
    return {"sub": sub, "window": window, "counter": counter, "policy": policy}


def _counter_row(item: dict, kind: str) -> dict:
    p = _plain(item)
    pk = p.pop("pk", "")
    p.pop("sk", None), p.pop("w", None)
    if kind == "user":
        p["sub"] = pk.removeprefix("USE#").rsplit("#", 1)[0]
    else:
        p["group"] = pk.removeprefix("GROUP#").rsplit("#", 1)[0]
    used = p.get("used_usd", 0.0) + max(0.0, p.get("est_usd", 0.0))
    hard = p.get("hard_limit_usd") or HARD_DEFAULT_USD
    p["total_usd"] = round(used, 6)
    p["hard_limit_usd"] = hard
    p["soft_limit_usd"] = p.get("soft_limit_usd") or SOFT_DEFAULT_USD
    p["pct_of_limit"] = round(100.0 * used / hard, 1) if hard > 0 else 0.0
    return p


def _query_window_counters(window: str, kind: str, limit: int, cursor: dict | None):
    prefix = "USE#" if kind == "user" else "GROUP#"
    kwargs = {
        "TableName": TABLE,
        "IndexName": "by-window",
        "KeyConditionExpression": "w = :w",
        "FilterExpression": "begins_with(pk, :pfx)",
        "ExpressionAttributeValues": {":w": {"S": window}, ":pfx": {"S": prefix}},
        "ScanIndexForward": False,  # highest settled spend first
        "Limit": min(limit * 3, 300),  # filter headroom: user+group share the partition
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    raw, exhausted = [], False
    while len(raw) < limit:
        page = ddb.query(**kwargs)
        raw.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            exhausted = True
            break
        kwargs["ExclusiveStartKey"] = lek
    raw = raw[:limit]
    rows = [_counter_row(i, kind) for i in raw]
    # Cursor from the last RETURNED item (truncating a filtered page and
    # reusing its LastEvaluatedKey would skip the tail of that page). A GSI
    # ExclusiveStartKey is the GSI keys + the table keys of that item.
    next_cursor = None
    if raw and not exhausted:
        last = raw[-1]
        next_cursor = {k: last[k] for k in ("w", "used_usd", "pk", "sk") if k in last}
    return rows, next_cursor


def _emails_for(subs: list) -> dict:
    """sub → {email, status} via AdminGetUser; parallel + container-cached."""
    if not USER_POOL_ID:
        return {}
    out = {s: _email_cache[s] for s in subs if s in _email_cache}
    missing = [s for s in subs if s not in out]

    def fetch(sub):
        try:
            u = _client("cognito-idp").admin_get_user(UserPoolId=USER_POOL_ID, Username=sub)
            attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
            return sub, {"email": attrs.get("email", ""), "status": u.get("UserStatus", ""), "enabled": u.get("Enabled", True)}
        except Exception:  # noqa: BLE001 — deleted users still have counters
            return sub, {"email": "", "status": "NOT_FOUND", "enabled": False}

    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for sub, info in ex.map(fetch, missing):
                out[sub] = info
                _email_cache[sub] = info
    return out


def _list_users(window: str, limit: int, cursor: str | None, flt: str | None) -> dict:
    rows, lek = _query_window_counters(window, "user", limit, _cursor_in(cursor))
    emails = _emails_for([r["sub"] for r in rows])
    for r in rows:
        r.update(emails.get(r["sub"], {}))
    if flt == "near-limit":
        rows = [r for r in rows if r["pct_of_limit"] >= 80.0]
    elif flt == "over-limit":
        rows = [r for r in rows if r["pct_of_limit"] >= 100.0]
    return {"window": window, "users": rows, "cursor": _cursor_out(lek)}


def _list_groups(window: str) -> dict:
    rows, _ = _query_window_counters(window, "group", 100, None)
    policies = _list_policies()["policies"]
    group_policies = {p["scope"].removeprefix("GROUP#"): p for p in policies if p["scope"].startswith("GROUP#")}
    for r in rows:
        gp = group_policies.get(r["group"])
        if gp:
            r["ceiling_usd"] = gp.get("hard_limit_usd")
            r["pct_of_ceiling"] = round(100.0 * r["total_usd"] / gp["hard_limit_usd"], 1) if gp.get("hard_limit_usd") else None
    return {"window": window, "groups": rows, "group_policies": group_policies}


# ── ledger / estimates / audit / activity ───────────────────────────────────

def _ledger_row(item: dict) -> dict:
    p = _plain(item)
    sk = p.pop("sk", "")
    p.pop("pk", None), p.pop("idem", None), p.pop("ttl", None)
    try:
        p["ts"] = int(sk.split("#", 1)[0])
    except ValueError:
        p["ts"] = 0
    return p


def _user_ledger(sub: str, limit: int, cursor: str | None) -> dict:
    kwargs = {
        "TableName": TABLE,
        "IndexName": "by-sub",
        "KeyConditionExpression": "#sub = :s",
        "FilterExpression": "begins_with(pk, :led)",
        "ExpressionAttributeNames": {"#sub": "sub"},
        "ExpressionAttributeValues": {":s": {"S": sub}, ":led": {"S": "LEDGER#"}},
        "ScanIndexForward": False,  # newest first (ledger sk starts with epoch)
        "Limit": min(limit * 2, 200),
    }
    cur = _cursor_in(cursor)
    if cur:
        kwargs["ExclusiveStartKey"] = cur
    raw, exhausted = [], False
    while len(raw) < limit:
        page = ddb.query(**kwargs)
        raw.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek:
            exhausted = True
            break
        kwargs["ExclusiveStartKey"] = lek
    raw = raw[:limit]
    next_cursor = None
    if raw and not exhausted:
        last = raw[-1]
        next_cursor = {k: last[k] for k in ("sub", "sk", "pk") if k in last}
    return {"sub": sub, "calls": [_ledger_row(i) for i in raw], "cursor": _cursor_out(next_cursor)}


def _open_estimates(sub: str | None = None, limit: int = 100) -> list:
    kwargs = {
        "TableName": TABLE,
        "IndexName": "estimates",
        "KeyConditionExpression": "#s = :open",
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": {":open": {"S": "OPEN"}},
        "ScanIndexForward": True,  # oldest first
        "Limit": limit,
    }
    if sub:
        kwargs["FilterExpression"] = "#sub = :sub"
        kwargs["ExpressionAttributeNames"]["#sub"] = "sub"
        kwargs["ExpressionAttributeValues"][":sub"] = {"S": sub}
    items = ddb.query(**kwargs).get("Items", [])
    out = []
    for it in items:
        p = _plain(it)
        p["estimate_key"] = p.pop("pk", "").removeprefix("EST#")
        p.pop("sk", None), p.pop("ttl", None), p.pop("state", None)
        out.append(p)
    return out


def _audit_trail(days: int, actor_filter: str | None) -> dict:
    rows = []
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for d in range(min(days, 31)):
        day = (today - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
        kwargs = {
            "TableName": TABLE,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": f"AUDIT#{day}"}},
            "ScanIndexForward": False,
        }
        if actor_filter:
            kwargs["FilterExpression"] = "actor = :a"
            kwargs["ExpressionAttributeValues"][":a"] = {"S": actor_filter}
        for it in ddb.query(**kwargs).get("Items", []):
            p = _plain(it)
            p.pop("pk", None), p.pop("ttl", None)
            try:
                p["ts"] = int(p.pop("sk", "0#").split("#", 1)[0])
            except ValueError:
                p["ts"] = 0
            for f in ("before", "after"):
                try:
                    p[f] = json.loads(p.get(f, "{}"))
                except (ValueError, TypeError):
                    pass
            rows.append(p)
        if len(rows) >= 200:
            break
    return {"entries": rows[:200], "days": days}


def _activity(limit: int) -> dict:
    rows = []
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for d in range(2):  # today + yesterday
        day = (today - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
        page = ddb.query(
            TableName=TABLE,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": {"S": f"LEDGER#{day}"}},
            ScanIndexForward=False,
            Limit=limit,
        )
        rows.extend(_ledger_row(i) for i in page.get("Items", []))
        if len(rows) >= limit:
            break
    rows = rows[:limit]
    emails = _emails_for(list({r.get("sub", "") for r in rows if r.get("sub")}))
    for r in rows:
        r["email"] = emails.get(r.get("sub", ""), {}).get("email", "")
    return {"calls": rows}


# ── cognito identity ────────────────────────────────────────────────────────

_SUB_RE = re.compile(r"^[0-9a-f-]{36}$")


def _search_users(q: str) -> dict:
    if not USER_POOL_ID:
        return {"users": [], "error": "USER_POOL_ID not configured"}
    cog = _client("cognito-idp")
    found = {}

    def add(u):
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", u.get("UserAttributes", []))}
        sub = attrs.get("sub", u.get("Username", ""))
        found[sub] = {
            "sub": sub,
            "email": attrs.get("email", ""),
            "name": attrs.get("name", ""),
            "status": u.get("UserStatus", ""),
            "enabled": u.get("Enabled", True),
        }

    # Cognito ListUsers filter uses " as the value delimiter with no escape
    # syntax, so strip quotes/backslashes and bound the length before
    # interpolating (admin-only route, but keep the query well-formed and
    # un-injectable regardless).
    safe_q = q.replace('"', "").replace("\\", "").strip()[:64]
    try:
        if _SUB_RE.match(q.lower()):
            try:
                u = cog.admin_get_user(UserPoolId=USER_POOL_ID, Username=q.lower())
                add(u)
            except cog.exceptions.UserNotFoundException:
                pass
        if safe_q:
            for it in cog.list_users(UserPoolId=USER_POOL_ID, Filter=f'email ^= "{safe_q}"', Limit=20).get("Users", []):
                add(it)
    except Exception as e:  # noqa: BLE001
        log.warning(f"user search failed: {e}")
        return {"users": [], "error": "search failed"}

    users = list(found.values())[:20]
    for u in users:
        try:
            gr = cog.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=u["sub"], Limit=10)
            u["groups"] = [g["GroupName"] for g in gr.get("Groups", [])]
        except Exception:  # noqa: BLE001
            u["groups"] = []
    return {"users": users}


def _user_detail(sub: str, window: str) -> dict:
    counter = _plain(
        ddb.get_item(
            TableName=TABLE, Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}}
        ).get("Item")
    )
    counter.pop("pk", None), counter.pop("sk", None), counter.pop("w", None)
    profile, groups = {}, []
    if USER_POOL_ID:
        cog = _client("cognito-idp")
        try:
            u = cog.admin_get_user(UserPoolId=USER_POOL_ID, Username=sub)
            attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
            profile = {
                "email": attrs.get("email", ""),
                "name": attrs.get("name", ""),
                "status": u.get("UserStatus", ""),
                "enabled": u.get("Enabled", True),
                "created": u.get("UserCreateDate", datetime.datetime.now(datetime.timezone.utc)).isoformat(),
            }
            gr = cog.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=sub, Limit=10)
            groups = [g["GroupName"] for g in gr.get("Groups", [])]
        except Exception:  # noqa: BLE001 — counters can outlive their user
            profile = {"email": "", "status": "NOT_FOUND", "enabled": False}
    used = counter.get("used_usd", 0.0) + max(0.0, counter.get("est_usd", 0.0))
    policy = _resolve_policy_chain(sub)
    hard = policy["effective"].get("hard_limit_usd", HARD_DEFAULT_USD)
    return {
        "sub": sub,
        "window": window,
        "profile": profile,
        "groups": groups,
        "is_admin_group_member": bool(ADMIN_GROUPS.intersection(groups)),
        "counter": counter,
        "total_usd": round(used, 6),
        "pct_of_limit": round(100.0 * used / hard, 1) if hard > 0 else 0.0,
        "policy": policy,
        "open_estimates": _open_estimates(sub=sub, limit=25),
        "recent_calls": _user_ledger(sub, 10, None)["calls"],
    }


# ── cloudwatch / sns ────────────────────────────────────────────────────────

_RANGES = {"3h": (3 * 3600, 300), "24h": (24 * 3600, 900), "7d": (7 * 86400, 3600), "30d": (30 * 86400, 21600)}
_SERIES = [
    ("spend_usd", "SettledUSD", "Sum", None),
    ("settled_calls", "SettledCalls", "Sum", None),
    ("denies", "DenyDecisions", "Sum", None),
    ("degraded_checks", "DegradedChecks", "Sum", None),
    ("swept_estimates", "SweptEstimates", "Sum", None),
    ("swept_usd", "SweptEstimateUSD", "Sum", None),
    ("block_canary_failures", "BlockCanaryFailure", "Sum", None),
    ("capture_canary_failures", "CaptureCanaryFailure", "Sum", None),
    ("drift_pct", "ReconciliationDriftPct", "Maximum", {"Model": "ALL"}),
]


def _metrics(range_key: str) -> dict:
    seconds, period = _RANGES.get(range_key, _RANGES["24h"])
    now = datetime.datetime.now(datetime.timezone.utc)
    queries = []
    for i, (key, name, stat, dims) in enumerate(_SERIES):
        metric = {"Namespace": "Metering", "MetricName": name}
        if dims:
            metric["Dimensions"] = [{"Name": k, "Value": v} for k, v in dims.items()]
        queries.append({
            "Id": f"m{i}",
            "Label": key,
            "MetricStat": {"Metric": metric, "Period": period, "Stat": stat},
        })
    res = _client("cloudwatch").get_metric_data(
        MetricDataQueries=queries,
        StartTime=now - datetime.timedelta(seconds=seconds),
        EndTime=now,
        ScanBy="TimestampAscending",
    )
    series = {}
    for r in res.get("MetricDataResults", []):
        series[r["Label"]] = [
            [int(t.timestamp()), v] for t, v in zip(r.get("Timestamps", []), r.get("Values", []))
        ]
    return {"range": range_key, "period_seconds": period, "series": series}


def _alarms() -> dict:
    res = _client("cloudwatch").describe_alarms(AlarmNamePrefix=ALARM_PREFIX, MaxRecords=50)
    alarms = []
    for a in res.get("MetricAlarms", []):
        alarms.append({
            "name": a["AlarmName"],
            "state": a["StateValue"],
            "reason": a.get("StateReason", ""),
            "since": a.get("StateUpdatedTimestamp", datetime.datetime.now(datetime.timezone.utc)).isoformat(),
            "metric": a.get("MetricName", ""),
        })
    order = {"ALARM": 0, "INSUFFICIENT_DATA": 1, "OK": 2}
    return {"alarms": sorted(alarms, key=lambda a: (order.get(a["state"], 3), a["name"]))}


def _alert_subscriptions() -> dict:
    if not ALERTS_TOPIC_ARN:
        return {"subscriptions": []}
    res = _client("sns").list_subscriptions_by_topic(TopicArn=ALERTS_TOPIC_ARN)
    return {
        "subscriptions": [
            {"arn": s["SubscriptionArn"], "protocol": s["Protocol"], "endpoint": s["Endpoint"],
             "pending": s["SubscriptionArn"] == "PendingConfirmation"}
            for s in res.get("Subscriptions", [])
        ]
    }


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _subscribe_alerts(email: str, actor: str) -> dict:
    if not _EMAIL_RE.match(email):
        return {"error": "invalid email"}
    _client("sns").subscribe(TopicArn=ALERTS_TOPIC_ARN, Protocol="email", Endpoint=email)
    _audit(actor, "SUBSCRIBE_ALERTS", email, {}, {"email": email})
    return {"subscribed": email, "note": "confirmation email sent by SNS"}


def _unsubscribe_alerts(arn: str, actor: str) -> dict:
    if not arn.startswith(ALERTS_TOPIC_ARN + ":"):
        return {"error": "subscription does not belong to the metering alerts topic"}
    _client("sns").unsubscribe(SubscriptionArn=arn)
    _audit(actor, "UNSUBSCRIBE_ALERTS", arn, {}, {})
    return {"unsubscribed": arn}


# ── pricing catalog (.kiro/specs/metering-pricing-single-source/design.md) ──
# PRICING#<model_id>/PUBLISHED  (refresher, AWS Price List — rates grid per-1M)
# PRICING#<model_id>/OVERRIDE   (operator-entered here; wins over PUBLISHED)
# PRICING#_ALIAS/<pl_name>      (operator binding: Price List name → model id)
# PRICING#_UNMATCHED/<pl_name>  (review queue: published rate, unresolved id)
# PRICING#_CATALOG/META         (refresh marker: versions, counts, generation)

# Per-1M validation bound for overrides: $0 … $1,000,000 per 1M tokens.
_MAX_OVERRIDE_PER_1M = 1e6


def _pricing_rows() -> list:
    """All PRICING# rows; the estimates GSI would miss them (no state attr);
    query the pk space directly with a Scan bounded to PRICING# — the catalog is
    a few hundred rows, not the ledger, so this is cheap and paginated defensively."""
    items, lek = [], None
    while True:
        kwargs = {
            "TableName": TABLE,
            "FilterExpression": "begins_with(pk, :p)",
            "ExpressionAttributeValues": {":p": {"S": "PRICING#"}},
        }
        if lek:
            kwargs["ExclusiveStartKey"] = lek
        page = ddb.scan(**kwargs)
        items.extend(page.get("Items", []))
        lek = page.get("LastEvaluatedKey")
        if not lek or len(items) > 5000:
            break
    return items


def _catalog() -> dict:
    """The pricing surface: priced models (rates grid + effective standard
    rate), the unmatched review queue, operator aliases, and refresh meta.

    Alias-materialized rows (alias_of set) fold into their canonical model's
    `keys` list so the table shows one row per model. Source labels are only
    the reachable ones: override | aws-published | unpriced (Req 9.5).
    """
    rows = _pricing_rows()
    models: dict = {}
    unmatched: list = []
    aliases: list = []
    meta: dict = {}
    for it in rows:
        p = unwrap_item(it)
        pk, sk = p.pop("pk", ""), p.pop("sk", "")
        key = pk.removeprefix("PRICING#")
        if key == "_CATALOG":
            meta = p
            continue
        if key == "_UNMATCHED":
            unmatched.append(p)
            continue
        if key == "_ALIAS":
            aliases.append({"price_list_name": sk, "model_id": p.get("model_id"),
                            "updated_by": p.get("updated_by"), "updated_at": p.get("updated_at")})
            continue
        if sk == "PUBLISHED":
            canonical = p.get("alias_of") or p.get("canonical_id") or key
            entry = models.setdefault(canonical, {"model": canonical, "keys": []})
            entry["keys"].append(key)
            if key == canonical or "published" not in entry:
                entry["published"] = p  # the canonical row's data wins
        elif sk == "OVERRIDE":
            entry = models.setdefault(key, {"model": key, "keys": []})
            entry["override"] = p
        # legacy PROVIDER/DEFAULT rows (pre-migration) are not part of the
        # pricing surface — the first refresh garbage-collects them
    out = []
    for model, e in sorted(models.items()):
        pub, ov = e.get("published"), e.get("override")
        entry = {"override": ov, "published": pub}
        res_in = resolve_rate(entry, "input")
        res_out = resolve_rate(entry, "output")
        eff_src = res_in.source if res_in.source != "unpriced" else res_out.source
        out.append({
            "model": model,
            "display_name": (pub or {}).get("display_name") or model,
            "provider": (pub or {}).get("provider", ""),
            "keys": sorted(set(e.get("keys") or [model])),
            "routing_modes": sorted((pub or {}).get("rates", {}).keys()) if pub else [],
            "rates": (pub or {}).get("rates"),
            "resolved_via": (pub or {}).get("resolved_via"),
            "price_list_name": (pub or {}).get("price_list_name"),
            "offer_version": (pub or {}).get("offer_version"),
            # effective standard in-region rate, USD per 1M tokens
            "effective": {"input_per_1m": res_in.usd_per_1m,
                          "output_per_1m": res_out.usd_per_1m,
                          "source": eff_src},
            "override": ov,
        })
    return {
        "models": out,
        "unmatched": sorted(unmatched, key=lambda u: str(u.get("price_list_name", ""))),
        "aliases": sorted(aliases, key=lambda a: str(a.get("price_list_name", ""))),
        "meta": meta,
        "count": len(out),
        "_UNIT": UNIT_PER_1M,
    }


def _put_price_override(model: str, body: dict, actor: str) -> dict:
    """Operator override, USD per 1M tokens per direction (Req 5.4, 5.7-5.9).

    Stored flat under scope=ALL: it applies to every tier, routing mode and
    context; the scope attribute lets a future tier-qualified override coexist
    without migrating rows. The model id must be settle-reachable (lowercase
    vendor.model) — an override keyed like a display name could never price.
    """
    if not MODEL_ID_RE.match(model):
        raise ValueError("invalid model id (expected a Bedrock id like vendor.model-name)")
    inp = body.get("input")
    out = body.get("output")
    if inp is None and out is None:
        raise ValueError("at least one of input/output (USD per 1M tokens) required")
    rates = {}
    for direction, v in (("input", inp), ("output", out)):
        if v is None:
            continue
        v = float(v)
        if not (0 <= v <= _MAX_OVERRIDE_PER_1M):
            raise ValueError(f"per-1M rate must be between 0 and {int(_MAX_OVERRIDE_PER_1M)} USD")
        rates[direction] = v
    before = _plain(ddb.get_item(
        TableName=TABLE, Key={"pk": {"S": f"PRICING#{model}"}, "sk": {"S": "OVERRIDE"}}, ConsistentRead=True,
    ).get("Item"))
    now = int(time.time())
    item = {
        "pk": {"S": f"PRICING#{model}"},
        "sk": {"S": "OVERRIDE"},
        "model_id": {"S": model},
        "source": {"S": "override"},
        "_UNIT": {"S": UNIT_PER_1M},
        "scope": {"S": "ALL"},
        "rates": {"M": {d: {"N": str(v)} for d, v in rates.items()}},
        "updated_by": {"S": actor},
        "updated_at": {"N": str(now)},
    }
    if body.get("note"):
        item["note"] = {"S": str(body["note"])[:500]}
    ddb.put_item(TableName=TABLE, Item=item)
    after = _plain(ddb.get_item(
        TableName=TABLE, Key={"pk": {"S": f"PRICING#{model}"}, "sk": {"S": "OVERRIDE"}}, ConsistentRead=True,
    ).get("Item"))
    _audit(actor, "PUT_PRICE_OVERRIDE", model, before, after)
    return after


def _delete_price_override(model: str, actor: str) -> dict:
    before = _plain(ddb.get_item(
        TableName=TABLE, Key={"pk": {"S": f"PRICING#{model}"}, "sk": {"S": "OVERRIDE"}}, ConsistentRead=True,
    ).get("Item"))
    if not before:
        return {"deleted": False, "reason": "no override for this model"}
    ddb.delete_item(TableName=TABLE, Key={"pk": {"S": f"PRICING#{model}"}, "sk": {"S": "OVERRIDE"}})
    _audit(actor, "DELETE_PRICE_OVERRIDE", model, before, {})
    return {"deleted": True, "model": model, "note": "model reverts to AWS-published rate (or unpriced)"}


def _put_alias(body: dict, actor: str) -> dict:
    """Bind a Price List name to a model id (Req 3.4). The binding outranks
    the automatic control-plane join on the next refresh — no redeploy."""
    name = str(body.get("price_list_name") or "").strip()
    model_id = str(body.get("model_id") or "").strip()
    if not name or len(name) > 128:
        raise ValueError("price_list_name required (max 128 chars)")
    if not MODEL_ID_RE.match(model_id):
        raise ValueError("invalid model id (expected a Bedrock id like vendor.model-name)")
    before = _plain(ddb.get_item(
        TableName=TABLE, Key={"pk": {"S": "PRICING#_ALIAS"}, "sk": {"S": name}}, ConsistentRead=True,
    ).get("Item"))
    now = int(time.time())
    ddb.put_item(TableName=TABLE, Item={
        "pk": {"S": "PRICING#_ALIAS"},
        "sk": {"S": name},
        "price_list_name": {"S": name},
        "model_id": {"S": model_id},
        "updated_by": {"S": actor},
        "updated_at": {"N": str(now)},
    })
    _audit(actor, "PUT_PRICING_ALIAS", name, before, {"model_id": model_id})
    return {"price_list_name": name, "model_id": model_id,
            "note": "applies on the next pricing refresh"}


def _delete_alias(name: str, actor: str) -> dict:
    name = str(name or "").strip()
    before = _plain(ddb.get_item(
        TableName=TABLE, Key={"pk": {"S": "PRICING#_ALIAS"}, "sk": {"S": name}}, ConsistentRead=True,
    ).get("Item"))
    if not before:
        return {"deleted": False, "reason": "no alias for this Price List name"}
    ddb.delete_item(TableName=TABLE, Key={"pk": {"S": "PRICING#_ALIAS"}, "sk": {"S": name}})
    _audit(actor, "DELETE_PRICING_ALIAS", name, before, {})
    return {"deleted": True, "price_list_name": name,
            "note": "the name returns to automatic resolution on the next refresh"}


def _pricing_meta() -> dict:
    """Catalog freshness for GET /config — read from the live refresh marker
    (PRICING#_CATALOG/META), not from a deploy-time constant (Req 8.3).
    Fail-soft: the console shell must render even if this read hiccups."""
    try:
        item = ddb.get_item(
            TableName=TABLE, Key={"pk": {"S": "PRICING#_CATALOG"}, "sk": {"S": "META"}},
        ).get("Item")
    except Exception as e:  # noqa: BLE001
        log.warning(f"pricing meta read failed: {e}")
        item = None
    if not item:
        return {"catalog_version": None, "refreshed_at": None}
    p = unwrap_item(item)
    return {
        "catalog_version": p.get("offer_versions") or p.get("version"),
        "refresh_generation": p.get("refresh_generation"),
        "model_count": p.get("model_count"),
        "unmatched_count": p.get("unmatched_count"),
        "refreshed_at": p.get("refreshed_at"),
        "partial": p.get("partial", False),
        "region": p.get("region"),
    }


def _refresh_pricing(actor: str) -> dict:
    """Trigger the pricing refresher Lambda synchronously (bounded work: ~2 offer
    files). Returns its result so the console shows the new model count."""
    if not PRICING_REFRESHER_FN:
        return {"error": "pricing refresher not configured"}
    resp = _client("lambda").invoke(FunctionName=PRICING_REFRESHER_FN, InvocationType="RequestResponse")
    payload = json.loads(resp["Payload"].read() or "{}")
    _audit(actor, "REFRESH_PRICING", "catalog", {}, payload if isinstance(payload, dict) else {})
    return payload


# ── handler ─────────────────────────────────────────────────────────────────

def handler(event, context):
    claims = _claims(event)
    actor = claims.get("sub", "unknown")
    route = event.get("routeKey", "")
    path_params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    window = _safe_window(qs)

    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return _resp(400, {"error": "invalid JSON body"})

    # ── self-service routes (any authenticated pool user) ──
    if route == "GET /usage/me":
        out = _usage(actor, window)
        out["resolved"] = _resolve_policy_chain(actor)
        return _resp(200, out)

    if route == "GET /user/me/ledger":
        return _resp(200, _user_ledger(actor, _int_param(qs, "limit", 25, 1, 100), qs.get("cursor")))

    if route == "GET /config":
        is_admin = _is_admin(claims)
        cfg = {
            "enforce_mode": ENFORCE_MODE,
            "pricing": _cached("config_pricing", _pricing_meta),
            "defaults": {"hard_limit_usd": HARD_DEFAULT_USD, "soft_limit_usd": SOFT_DEFAULT_USD, "rpm_limit": RPM_DEFAULT},
            "window": _window_now(),
            "is_admin": is_admin,
        }
        # Don't disclose which groups confer admin to non-admins — the pool
        # allows self-signup, so this would be reconnaissance (review L2).
        if is_admin:
            cfg["admin_groups"] = sorted(ADMIN_GROUPS)
        return _resp(200, cfg)

    if not _is_admin(claims):
        return _resp(403, {"error": "admin group membership required"})

    # ── admin reads ──
    if route == "GET /policy/{scope}":
        return _resp(200, _get_policy(path_params.get("scope", "DEFAULT")) or {})

    if route == "GET /policies":
        return _resp(200, _cached("policies", _list_policies))

    if route == "GET /usage/{sub}":
        return _resp(200, _usage(path_params.get("sub", ""), window))

    if route == "GET /users":
        limit = _int_param(qs, "limit", 50, 1, 100)
        cursor, flt = qs.get("cursor"), qs.get("filter")
        if cursor or flt:
            return _resp(200, _list_users(window, limit, cursor, flt))
        return _resp(200, _cached(f"users#{window}#{limit}", lambda: _list_users(window, limit, None, None)))

    if route == "GET /users/search":
        q = (qs.get("q") or "").strip()
        if len(q) < 2:
            return _resp(400, {"error": "q must be at least 2 characters"})
        return _resp(200, _search_users(q))

    if route == "GET /user/{sub}":
        return _resp(200, _user_detail(path_params.get("sub", ""), window))

    if route == "GET /user/{sub}/ledger":
        return _resp(200, _user_ledger(
            path_params.get("sub", ""), _int_param(qs, "limit", 25, 1, 100), qs.get("cursor")))

    if route == "GET /groups":
        return _resp(200, _cached(f"groups#{window}", lambda: _list_groups(window)))

    if route == "GET /audit":
        return _resp(200, _audit_trail(_int_param(qs, "days", 7, 1, 31), qs.get("actor")))

    if route == "GET /activity":
        return _resp(200, _activity(_int_param(qs, "limit", 50, 1, 100)))

    if route == "GET /estimates":
        return _resp(200, {"estimates": _open_estimates(limit=_int_param(qs, "limit", 100, 1, 200))})

    if route == "GET /metrics":
        return _resp(200, _cached(f"metrics#{qs.get('range', '24h')}", lambda: _metrics(qs.get("range", "24h"))))

    if route == "GET /alarms":
        return _resp(200, _cached("alarms", _alarms))

    if route == "GET /alert-subscriptions":
        return _resp(200, _alert_subscriptions())

    if route == "GET /pricing":
        return _resp(200, _cached("pricing", _catalog))

    # ── admin mutations (audited; self-target rejected) ──
    if route == "PUT /policy/{scope}":
        scope = path_params.get("scope", "")
        if not scope or "hard_limit_usd" not in body:
            return _resp(400, {"error": "scope path param and hard_limit_usd required"})
        if scope == f"USER#{actor}":
            return _resp(403, {"error": "self-targeted policy changes are rejected; a second admin must act"})
        try:
            return _resp(200, _put_policy(scope, body, actor))
        except (ValueError, TypeError) as e:
            return _resp(400, {"error": str(e)})

    if route == "DELETE /policy/{scope}":
        scope = path_params.get("scope", "")
        if not scope:
            return _resp(400, {"error": "scope path param required"})
        if scope == f"USER#{actor}":
            return _resp(403, {"error": "self-targeted policy changes are rejected; a second admin must act"})
        return _resp(200, _delete_policy(scope, actor))

    if route == "POST /override":
        sub = body.get("sub", "")
        if not sub or "hard_limit_usd" not in body:
            return _resp(400, {"error": "sub and hard_limit_usd required"})
        if sub == actor:
            return _resp(403, {"error": "self-targeted overrides are rejected; a second admin must act"})
        try:
            return _resp(200, _put_policy(f"USER#{sub}", body, actor))
        except (ValueError, TypeError) as e:
            return _resp(400, {"error": str(e)})

    if route == "POST /counter-reset":
        sub = body.get("sub", "")
        if not sub:
            return _resp(400, {"error": "sub required"})
        if sub == actor:
            return _resp(403, {"error": "self-targeted resets are rejected; a second admin must act"})
        w = body.get("window") or window
        if not _WINDOW_RE.match(w):
            return _resp(400, {"error": "window must be YYYY-MM"})
        key = {"pk": {"S": f"USE#{sub}#{w}"}, "sk": {"S": "COUNTER"}}
        # Strongly consistent so the audit row's "before" reflects the true
        # pre-reset counter, not a stale replica (review finding).
        before = _plain(ddb.get_item(TableName=TABLE, Key=key, ConsistentRead=True).get("Item"))
        ddb.update_item(
            TableName=TABLE,
            Key=key,
            UpdateExpression="SET used_usd = :z, est_usd = :z, alerted = :none, updated_at = :now, w = :w",
            ExpressionAttributeValues={
                ":z": {"N": "0"},
                ":none": {"S": ""},
                ":now": {"N": str(int(time.time()))},
                ":w": {"S": w},
            },
        )
        _audit(actor, "COUNTER_RESET", f"{sub}#{w}", before, {"used_usd": 0, "est_usd": 0})
        return _resp(200, {"reset": True, "sub": sub, "window": w})

    if route == "POST /alert-subscriptions":
        email = (body.get("email") or "").strip()
        if not email:
            return _resp(400, {"error": "email required"})
        out = _subscribe_alerts(email, actor)
        return _resp(400 if "error" in out else 200, out)

    if route == "DELETE /alert-subscriptions":
        arn = (qs.get("arn") or "").strip()
        if not arn:
            return _resp(400, {"error": "arn query param required"})
        out = _unsubscribe_alerts(arn, actor)
        return _resp(400 if "error" in out else 200, out)

    if route == "POST /pricing/alias":
        try:
            return _resp(200, _put_alias(body, actor))
        except ValueError as e:
            return _resp(400, {"error": str(e)})

    if route == "DELETE /pricing/alias/{name}":
        return _resp(200, _delete_alias(path_params.get("name", ""), actor))

    if route == "PUT /pricing/{model}":
        try:
            return _resp(200, _put_price_override(path_params.get("model", ""), body, actor))
        except (ValueError, TypeError) as e:
            return _resp(400, {"error": str(e)})

    if route == "DELETE /pricing/{model}":
        return _resp(200, _delete_price_override(path_params.get("model", ""), actor))

    if route == "POST /pricing/refresh":
        out = _refresh_pricing(actor)
        return _resp(400 if isinstance(out, dict) and "error" in out else 200, out)

    return _resp(404, {"error": f"unknown route {route}"})
