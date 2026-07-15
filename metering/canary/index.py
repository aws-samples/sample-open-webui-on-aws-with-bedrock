# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metering canaries — prove both failure directions hourly (design §4.2/§4.8).

Prior art shows metering and enforcement regress INDEPENDENTLY (LiteLLM
#26672: spend tracked, blocking silently gone), so one canary per direction:

  MODE=block    a synthetic user pinned over a ~zero quota calls the gateway;
                anything other than HTTP 429/quota_exceeded ⇒ BlockCanaryFailure.
  MODE=capture  a synthetic user emits a usage event to the metering bus and
                asserts its counter settles (req_count increments) within 60 s;
                no settle ⇒ CaptureCanaryFailure. This exercises the
                bus → debit → transactional-settle pipeline the seeded filter
                feeds. (Filter emission itself is proven at deploy-verify and
                watched by the capture-liveness alarm.)

The canary manages its own Cognito user at runtime (AdminGetUser →
AdminCreateUser + AdminSetUserPassword from a Secrets Manager password) and
discovers the gateway URL from SSM — both written by the gateway stack —
so no CloudFormation cycle exists between the metering and gateway stacks.
Canary traffic is tagged source=CANARY and excluded from spend metrics.
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

MODE = os.environ["MODE"]  # block | capture
TABLE = os.environ["TABLE"]
BUS = os.environ.get("BUS", "")
USER_POOL_ID = os.environ["USER_POOL_ID"]
CLIENT_ID = os.environ["CLIENT_ID"]
PASSWORD_SECRET_ARN = os.environ["PASSWORD_SECRET_ARN"]
GATEWAY_URL_PARAM = os.environ["GATEWAY_URL_PARAM"]
USERNAME = os.environ.get("USERNAME", f"metering-{MODE}-canary")
MODEL = os.environ.get("CANARY_MODEL", "qwen.qwen3-32b")
TARGET_PREFIX = os.environ.get("TARGET_PREFIX", "bedrock/")

idp = boto3.client("cognito-idp")
ddb = boto3.client("dynamodb")
ssm = boto3.client("ssm")
sm = boto3.client("secretsmanager")
events = boto3.client("events")
cw = boto3.client("cloudwatch")


def _metric(name: str, value: float = 1):
    cw.put_metric_data(
        Namespace="Metering",
        MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
    )


def _password() -> str:
    return sm.get_secret_value(SecretId=PASSWORD_SECRET_ARN)["SecretString"]


def _ensure_user() -> None:
    try:
        idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=USERNAME)
    except idp.exceptions.UserNotFoundException:
        idp.admin_create_user(UserPoolId=USER_POOL_ID, Username=USERNAME, MessageAction="SUPPRESS")
        idp.admin_set_user_password(
            UserPoolId=USER_POOL_ID, Username=USERNAME, Password=_password(), Permanent=True
        )


def _token() -> str:
    resp = idp.initiate_auth(
        ClientId=CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": USERNAME, "PASSWORD": _password()},
    )
    return resp["AuthenticationResult"]["AccessToken"]


def _sub(token: str) -> str:
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return claims["sub"]


def _ensure_policy(sub: str, hard_usd: str):
    ddb.put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": f"POLICY#USER#{sub}"},
            "sk": {"S": "POLICY"},
            "hard_limit_usd": {"N": hard_usd},
            "soft_limit_usd": {"N": hard_usd},
            "rpm_limit": {"N": "60"},
            "note": {"S": f"metering {MODE} canary policy (managed by the canary Lambda)"},
        },
        ConditionExpression="attribute_not_exists(pk)",
    )


def _gateway_url() -> str:
    return ssm.get_parameter(Name=GATEWAY_URL_PARAM)["Parameter"]["Value"].rstrip("/")


def _chat(token: str, url: str) -> tuple[int, dict]:
    body = json.dumps(
        {
            "model": f"{TARGET_PREFIX}{MODEL}",
            "messages": [{"role": "user", "content": "canary"}],
            "max_tokens": 4,
        }
    ).encode()
    req = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:  # noqa: S310 — https gateway URL from SSM
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except (ValueError, TypeError):
            return e.code, {}


def _counter(sub: str, window: str) -> dict:
    return ddb.get_item(
        TableName=TABLE,
        Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
        ConsistentRead=True,
    ).get("Item") or {}


def run_block(sub: str, token: str):
    # pin the canary over its limit so EVERY call must be denied
    window = time.strftime("%Y-%m", time.gmtime())
    _ensure_policy(sub, "0.000001")
    ddb.update_item(
        TableName=TABLE,
        Key={"pk": {"S": f"USE#{sub}#{window}"}, "sk": {"S": "COUNTER"}},
        UpdateExpression="SET used_usd = if_not_exists(used_usd, :one)",
        ExpressionAttributeValues={":one": {"N": "1"}},
    )
    status, body = _chat(token, _gateway_url())
    code = ((body or {}).get("error") or {}).get("code", "")
    if status == 429 and code == "quota_exceeded":
        _metric("BlockCanaryOK")
        log.info("block canary OK (429 quota_exceeded)")
    else:
        _metric("BlockCanaryFailure")
        log.error(f"BLOCK CANARY FAILED: status={status} code={code} body={json.dumps(body)[:300]}")


def run_capture(sub: str, token: str):
    _ensure_policy(sub, "1000")
    window = time.strftime("%Y-%m", time.gmtime())
    before = int(_counter(sub, window).get("req_count", {}).get("N", "0"))
    events.put_events(
        Entries=[
            {
                "Source": "openwebui.metering",
                "DetailType": "usage",
                "EventBusName": BUS,
                "Detail": json.dumps(
                    {
                        "sub": sub,
                        "groups": ["canary"],
                        "model": MODEL,
                        "response_id": f"canary-{uuid.uuid4().hex}",
                        "input_tokens": 1000,
                        "output_tokens": 100,
                        "source": "CANARY",
                        "ts": int(time.time()),
                    }
                ),
            }
        ]
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        after = int(_counter(sub, window).get("req_count", {}).get("N", "0"))
        if after > before:
            _metric("CaptureCanaryOK")
            log.info(f"capture canary OK (req_count {before}→{after})")
            return
        time.sleep(5)
    _metric("CaptureCanaryFailure")
    log.error("CAPTURE CANARY FAILED: usage event did not settle within 60s")


def handler(event, context):
    _ensure_user()
    token = _token()
    sub = _sub(token)
    if MODE == "block":
        run_block(sub, token)
    else:
        run_capture(sub, token)
