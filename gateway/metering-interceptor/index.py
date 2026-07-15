# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore Gateway REQUEST interceptor v2 — capability filter + quota enforcement.

Superset of gateway/interceptor/index.py (the base sample's models filter),
installed on the gateway ONLY when the metering module is enabled. Behavior
per request (design: docs/plans/metering-enforcement/02-DESIGN.md §4.2):

  GET …/models            → capability-filtered synthetic list (unchanged)
  POST inference paths    → 1. verify the caller's JWT (JWKS, refresh-on-kid)
                            2. read quota + RPM state (DynamoDB, ~3-5 ms)
                            3. over hard limit / RPM exhausted → 429 short-circuit
                            4. else: clamp per-lane max tokens, force
                               stream_options.include_usage (chat lane), inject
                               the per-team attribution header, write the
                               admission floor-debit estimate, forward
  anything else           → passthrough

Failure posture (§4.2): the PLATFORM is fail-closed (an unhandled exception
blocks the request and leaks stack traces — spike S7), so this module inverts
it in code: all metering logic runs inside a catch-all that ALLOWS the request
under a bounded per-subject grace budget and emits Metering/DegradedChecks.
A 429 is returned only on a positively-read exceeded counter. The outermost
handler returns a fixed opaque error if even the wrapper fails.

ENFORCE_MODE=OBSERVE logs every would-be decision without blocking (the
runbook's G2 ramp); ENFORCE is the real wall.
"""

import base64
import hashlib
import json
import logging
import os
import time
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

import boto3

# ── Config ──────────────────────────────────────────────────────────────────
# Large config (capability matrix, price map) ships as JSON files bundled into
# this Lambda's asset by the CDK gateway stack — the 4 KB env-var ceiling can't
# hold both. Env vars override for tests.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _bundled(name: str) -> dict:
    try:
        with open(os.path.join(_HERE, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CAPS = json.loads(os.environ["MODEL_CAPS"]) if os.environ.get("MODEL_CAPS") else _bundled("model-capabilities.json")
TARGET_PREFIX = os.environ.get("TARGET_PREFIX", "bedrock/")
DEFAULT_FLAVOR = "chat_completions"

TABLE = os.environ.get("TABLE", "")
JWKS_URL = os.environ.get("JWKS_URL", "")  # https://cognito-idp.{r}.amazonaws.com/{pool}/.well-known/jwks.json
ENFORCE_MODE = os.environ.get("ENFORCE_MODE", "OBSERVE")  # OBSERVE | ENFORCE
MAX_TOKENS_CLAMP = int(os.environ.get("MAX_TOKENS_CLAMP", "8192"))
RPM_LIMIT_DEFAULT = int(os.environ.get("RPM_LIMIT_DEFAULT", "30"))
GRACE_REQUESTS = int(os.environ.get("GRACE_REQUESTS", "10"))
HARD_DEFAULT_USD = float(os.environ.get("HARD_DEFAULT_USD", "5"))
SOFT_DEFAULT_USD = float(os.environ.get("SOFT_DEFAULT_USD", "4"))
# group->project header injection map: {"cognito-group": "proj_...", "*": "proj_default"}
PROJECT_MAP = json.loads(os.environ.get("PROJECT_MAP", "{}"))
# billing-group precedence (config/metering-groups.json order; JSON dicts through
# CloudFormation don't guarantee order, so the list travels separately)
GROUP_ORDER = [g for g in json.loads(os.environ.get("GROUP_ORDER", "[]")) if g != "*"] or [
    g for g in PROJECT_MAP if g != "*"
]
# conservative admission input estimate: bytes/4 (see track-d; no tokenizer in-path)
EST_INPUT_DIVISOR = 4
# price used for the floor estimate when the model is unpriced (worst-case-ish)
EST_FALLBACK_IN = float(os.environ.get("EST_FALLBACK_IN_PER_TOKEN", "3e-06"))
EST_FALLBACK_OUT = float(os.environ.get("EST_FALLBACK_OUT_PER_TOKEN", "1.5e-05"))
PRICE_MAP = (
    json.loads(os.environ["PRICE_MAP"]) if os.environ.get("PRICE_MAP") else _bundled("model-prices.json")
).get("models", {})

INFERENCE_SUFFIXES = ("/chat/completions", "/responses", "/messages")

ddb = boto3.client("dynamodb") if TABLE else None
cw = boto3.client("cloudwatch")

# ── JWKS cache (refresh on unknown kid; never fail-open per-request unbounded) ─
_jwks: dict = {"keys": {}, "fetched": 0.0}
# per-container degradation grace tally {sub: count}, reset every 5 minutes —
# a sustained store outage degrades to RATE-LIMITED fail-open (grace budget per
# window), never a permanent block (the deploy-order gap would otherwise 429
# users between gateway-deploy and metering-table creation)
_grace: dict = {}
_grace_window: list = [0]


def _metric(name: str, value: float = 1, dims: dict | None = None):
    try:
        cw.put_metric_data(
            Namespace="Metering",
            MetricData=[{
                "MetricName": name, "Value": value, "Unit": "Count",
                "Dimensions": [{"Name": k, "Value": v} for k, v in (dims or {}).items()],
            }],
        )
    except Exception:  # noqa: BLE001 — metrics must never affect the data path
        pass


def _b64url_to_int(s: str) -> int:
    pad = "=" * (-len(s) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(s + pad), "big")


def _fetch_jwks():
    with urllib.request.urlopen(JWKS_URL, timeout=3) as r:  # noqa: S310 — fixed https URL from env
        data = json.loads(r.read())
    _jwks["keys"] = {k["kid"]: k for k in data.get("keys", [])}
    _jwks["fetched"] = time.time()


def _verify_jwt(token: str) -> dict | None:
    """RS256-verify the access token against the pool JWKS. Returns claims or None.

    Signature verification is defense-in-depth — the gateway already validated
    the token before invoking us (spike S1) — so a verification FAILURE is
    treated as attribution-unknown, not as a user-facing rejection.
    """
    try:
        h_b64, p_b64, sig_b64 = token.split(".")
        header = json.loads(base64.urlsafe_b64decode(h_b64 + "=" * (-len(h_b64) % 4)))
        kid = header.get("kid", "")
        if kid not in _jwks["keys"] and (time.time() - _jwks["fetched"] > 60 or not _jwks["keys"]):
            _fetch_jwks()  # refresh-on-unknown-kid (Cognito key rotation)
        jwk = _jwks["keys"].get(kid)
        if not jwk or header.get("alg") != "RS256" or jwk.get("kty") != "RSA":
            return None
        n, e = _b64url_to_int(jwk["n"]), _b64url_to_int(jwk["e"])
        sig = int.from_bytes(base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4)), "big")
        # RSASSA-PKCS1-v1_5 verify with SHA-256 (stdlib-only; no crypto deps in-path)
        em = pow(sig, e, n).to_bytes((n.bit_length() + 7) // 8, "big")
        digest = hashlib.sha256(f"{h_b64}.{p_b64}".encode()).digest()
        # EMSA-PKCS1-v1_5: 0x00 0x01 PS(0xff…) 0x00 DigestInfo(SHA-256) || digest
        der_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
        expected_tail = der_prefix + digest
        if not (em[0] == 0 and em[1] == 1 and em.endswith(expected_tail)):
            return None
        ps = em[2:-(len(expected_tail) + 1)]
        if em[-(len(expected_tail) + 1)] != 0 or any(b != 0xFF for b in ps):
            return None
        claims = json.loads(base64.urlsafe_b64decode(p_b64 + "=" * (-len(p_b64) % 4)))
        if claims.get("exp", 0) < time.time() - 60:
            return None
        return claims
    except Exception:  # noqa: BLE001
        return None


# ── passthrough / short-circuit helpers ─────────────────────────────────────

def _passthrough(req):
    out = {}
    if req.get("body") is not None:
        out["transformedGatewayRequest"] = {"body": req["body"]}
    return {"interceptorOutputVersion": "1.0", "http": out}


def _short_circuit(status: int, body: dict, headers: dict | None = None):
    resp = {
        "statusCode": status,
        "contentType": "application/json",
        "body": base64.b64encode(json.dumps(body).encode()).decode(),
    }
    if headers:
        resp["headers"] = headers
    return {"interceptorOutputVersion": "1.0", "http": {"transformedGatewayResponse": resp}}


def _quota_error(reset_hint: str):
    return {
        "error": {
            "message": (
                f"Monthly AI budget reached ({reset_hint}). "
                "Contact your administrator to raise the limit."
            ),
            "type": "quota_exceeded",
            "code": "quota_exceeded",
        }
    }


# ── models listing (unchanged behavior from the base interceptor) ───────────

def _models_response(headers: dict):
    flavor = headers.get("x-models-flavor", DEFAULT_FLAVOR)
    allow = CAPS.get(flavor, CAPS.get(DEFAULT_FLAVOR, []))
    body = {
        "object": "list",
        "data": [
            {"id": f"{TARGET_PREFIX}{m}", "object": "model", "owned_by": "system"}
            for m in sorted(allow)
        ],
    }
    return _short_circuit(200, body)


# ── quota + admission ───────────────────────────────────────────────────────

def _decode_body(raw):
    if not raw:
        return None, False
    if isinstance(raw, str):
        try:
            return json.loads(raw), False
        except (ValueError, TypeError):
            pass
        try:
            return json.loads(base64.b64decode(raw)), True
        except Exception:  # noqa: BLE001
            return None, False
    return None, False


def _encode_body(parsed: dict, was_b64: bool) -> str:
    raw = json.dumps(parsed)
    return base64.b64encode(raw.encode()).decode() if was_b64 else raw


def _est_rate(model: str, direction: str) -> float:
    entry = PRICE_MAP.get(model) or {}
    v = (entry.get("standard") or {}).get(direction)
    if v is not None:
        return float(v)
    return EST_FALLBACK_IN if direction == "input" else EST_FALLBACK_OUT


def _read_state(sub: str, window: str, minute: str):
    """One BatchGetItem: user counter + policy rows + rpm bucket."""
    keys = [
        {"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
        {"pk": {"S": f"POLICY#USER#{sub}"}, "sk": {"S": "POLICY"}},
        {"pk": {"S": "POLICY#DEFAULT"}, "sk": {"S": "POLICY"}},
        {"pk": {"S": f"RPM#{sub}#{minute}"}, "sk": {"S": "RPM"}},
    ]
    resp = ddb.batch_get_item(RequestItems={TABLE: {"Keys": keys, "ConsistentRead": False}})
    items = {i["pk"]["S"]: i for i in resp.get("Responses", {}).get(TABLE, [])}
    return items


def _admit(sub: str, groups: list, body_bytes: int, parsed: dict, path: str) -> tuple[bool, dict | None, float]:
    """Returns (allowed, deny_body_or_None, est_usd)."""
    now = int(time.time())
    window = time.strftime("%Y-%m", time.gmtime(now))
    minute = time.strftime("%Y%m%d%H%M", time.gmtime(now))
    items = _read_state(sub, window, minute)

    counter = items.get(f"USE#{sub}#{window}", {})
    used = float(counter.get("used_usd", {}).get("N", "0")) + max(
        0.0, float(counter.get("est_usd", {}).get("N", "0"))
    )
    policy = items.get(f"POLICY#USER#{sub}") or items.get("POLICY#DEFAULT") or {}
    hard = float(policy.get("hard_limit_usd", {}).get("N", str(HARD_DEFAULT_USD)))
    rpm_limit = int(policy.get("rpm_limit", {}).get("N", str(RPM_LIMIT_DEFAULT)))
    rpm_used = int(items.get(f"RPM#{sub}#{minute}", {}).get("n", {}).get("N", "0"))

    # estimate: bytes/4 input + per-lane clamped output, priced (worst-case bound)
    model = str(parsed.get("model", "")).split("/", 1)[-1]
    est_in_tokens = max(1, body_bytes // EST_INPUT_DIVISOR)
    if path.endswith("/responses"):
        out_field = "max_output_tokens"
    else:
        out_field = "max_tokens"
    est_out_tokens = min(int(parsed.get(out_field) or MAX_TOKENS_CLAMP), MAX_TOKENS_CLAMP)
    est_usd = round(est_in_tokens * _est_rate(model, "input") + est_out_tokens * _est_rate(model, "output"), 8)

    if hard > 0 and used >= hard:
        return False, _quota_error("resets on the 1st"), est_usd
    if rpm_limit > 0 and rpm_used >= rpm_limit:
        return False, {
            "error": {
                "message": f"Rate limit: {rpm_limit} requests/minute. Retry shortly.",
                "type": "rate_limit_exceeded",
                "code": "rate_limit_exceeded",
            }
        }, est_usd
    return True, None, est_usd


def _billing_group(groups: list) -> str:
    """First CONFIGURED group wins (config/metering-groups.json order), so
    per-group invoices are deterministic for multi-group users (design §4.3)."""
    for configured in GROUP_ORDER:
        if configured in groups:
            return configured
    return "unassigned"


def _floor_debit(sub: str, groups: list, est_usd: float, parsed: dict, path: str, body_hash: str):
    """Idempotent admission estimate + RPM tick. Hash key survives gateway retries."""
    now = int(time.time())
    window = time.strftime("%Y-%m", time.gmtime(now))
    minute = time.strftime("%Y%m%d%H%M", time.gmtime(now))
    est_key = hashlib.sha256(f"{sub}#{body_hash}#{minute}".encode()).hexdigest()[:32]
    model = str(parsed.get("model", "")).split("/", 1)[-1]
    lane = next((s.strip("/") for s in INFERENCE_SUFFIXES if path.endswith(s)), "unknown")
    billing_group = _billing_group(groups)
    # RPM tick FIRST — every admitted request counts against the rate bucket,
    # including gateway retries and identical resends (else repeat traffic is
    # invisible to rate limiting).
    ddb.update_item(
        TableName=TABLE,
        Key={"pk": {"S": f"RPM#{sub}#{minute}"}, "sk": {"S": "RPM"}},
        UpdateExpression="ADD n :one SET #t = :ttl",
        ExpressionAttributeNames={"#t": "ttl"},
        ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(now + 120)}},
    )
    try:
        ddb.put_item(
            TableName=TABLE,
            Item={
                "pk": {"S": f"EST#est#{est_key}"},
                "sk": {"S": "EST"},
                "state": {"S": "OPEN"},
                "created_at": {"N": str(now)},
                "sub": {"S": sub},
                "window": {"S": window},
                "billing_group": {"S": billing_group},
                "model": {"S": model},
                "lane": {"S": lane},
                "usd": {"N": str(est_usd)},
                "ttl": {"N": str(now + 7 * 24 * 3600)},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ddb.exceptions.ConditionalCheckFailedException:
        return  # duplicate admission (gateway retry) — estimate already in force
    ddb.update_item(
        TableName=TABLE,
        Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
        UpdateExpression="ADD est_usd :e SET updated_at = :now",
        ExpressionAttributeValues={":e": {"N": str(est_usd)}, ":now": {"N": str(now)}},
    )


def _mutate_request(parsed: dict, path: str, claims: dict) -> dict:
    """Clamp per-lane output tokens, force include_usage, inject project header."""
    if path.endswith("/responses"):
        field = "max_output_tokens"
    else:
        field = "max_tokens"
    current = parsed.get(field)
    if current is None or int(current) > MAX_TOKENS_CLAMP:
        parsed[field] = MAX_TOKENS_CLAMP
    if path.endswith("/chat/completions") and parsed.get("stream"):
        so = parsed.get("stream_options") or {}
        so["include_usage"] = True
        parsed["stream_options"] = so
    return parsed


def _project_header(path: str, claims: dict) -> dict:
    if not PROJECT_MAP:
        return {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    proj = PROJECT_MAP.get(_billing_group(groups)) or PROJECT_MAP.get("*")
    if not proj:
        return {}
    # Lane-specific attribution header (spike S6: Messages rejects OpenAI-Project)
    header = "anthropic-workspace-id" if path.endswith("/messages") else "OpenAI-Project"
    return {header: proj}


# ── main handler ────────────────────────────────────────────────────────────

def _handle(event):
    http = event.get("http") or {}
    if http.get("gatewayResponse"):
        return {"interceptorOutputVersion": "1.0", "http": {}}
    req = http.get("gatewayRequest") or {}
    path = req.get("path", "")
    headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}

    # 1. models listing: capability filter (NEVER quota-blocked — discovery stays up)
    if path.rstrip("/").endswith("/models"):
        return _models_response(headers)

    # 2. non-inference paths: passthrough untouched
    if not any(path.rstrip("/").endswith(s) for s in INFERENCE_SUFFIXES):
        return _passthrough(req)

    # 3. inference: identity → quota → mutate-and-forward
    sub, groups, claims = "unknown", [], {}
    token = headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token and JWKS_URL:
        claims = _verify_jwt(token) or {}
        sub = claims.get("sub", "unknown")
        g = claims.get("cognito:groups") or []
        groups = [g] if isinstance(g, str) else list(g)

    parsed, was_b64 = _decode_body(req.get("body"))
    if parsed is None or not ddb or sub == "unknown":
        # No body to reason about / no table / no verified identity:
        # forward untouched but count the degraded check (bounded).
        if sub == "unknown" and token:
            _metric("DegradedChecks", dims={"Reason": "jwt"})
        return _passthrough(req)

    body_bytes = len(json.dumps(parsed).encode())
    body_hash = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()[:16]

    try:
        allowed, deny_body, est_usd = _admit(sub, groups, body_bytes, parsed, path)
    except Exception as e:  # noqa: BLE001 — quota-store degradation: bounded fail-open
        win = int(time.time() // 300)
        if _grace_window[0] != win:
            _grace_window[0] = win
            _grace.clear()
        n = _grace.get(sub, 0) + 1
        _grace[sub] = n
        _metric("DegradedChecks", dims={"Reason": "store"})
        log.warning(f"quota check degraded for {sub} ({e.__class__.__name__}); grace {n}/{GRACE_REQUESTS}")
        if n > GRACE_REQUESTS and ENFORCE_MODE == "ENFORCE":
            return _short_circuit(429, _quota_error("service degraded — grace budget exhausted"))
        return _passthrough(req)

    decision = "allow" if allowed else "deny"
    log.info(json.dumps({
        "decision": decision, "mode": ENFORCE_MODE, "sub": sub, "path": path,
        "est_usd": est_usd, "groups": groups[:5],
    }))

    if not allowed:
        _metric("DenyDecisions", dims={"Mode": ENFORCE_MODE})
        if ENFORCE_MODE == "ENFORCE":
            hdrs = {"Retry-After": "60"} if deny_body["error"]["code"] == "rate_limit_exceeded" else None
            return _short_circuit(429, deny_body, hdrs)
        # OBSERVE mode: log-only, forward

    try:
        _floor_debit(sub, groups, est_usd, parsed, path, body_hash)
    except Exception as e:  # noqa: BLE001 — estimate write failure never blocks
        _metric("DegradedChecks", dims={"Reason": "estimate"})
        log.warning(f"floor debit failed for {sub}: {e.__class__.__name__}")

    parsed = _mutate_request(parsed, path, claims)
    out_headers = _project_header(path, claims)
    out = {"body": _encode_body(parsed, was_b64)}
    if out_headers:
        out["headers"] = out_headers
    return {"interceptorOutputVersion": "1.0", "http": {"transformedGatewayRequest": out}}


def lambda_handler(event, context):
    # Outermost guard: platform is fail-closed on exceptions AND leaks stack
    # traces (spike S7) — anything escaping _handle returns opaque passthrough.
    try:
        return _handle(event)
    except Exception as e:  # noqa: BLE001
        log.error(f"interceptor outer failure: {e.__class__.__name__}: {e}")
        _metric("DegradedChecks", dims={"Reason": "outer"})
        try:
            req = (event.get("http") or {}).get("gatewayRequest") or {}
            return _passthrough(req)
        except Exception:  # noqa: BLE001
            return {"interceptorOutputVersion": "1.0", "http": {}}
