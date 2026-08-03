# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Interceptor v2 unit tests against the REAL gateway event shape (spike S1).

DynamoDB is stubbed with a tiny in-memory double; JWT verification is
exercised with a locally-generated RSA keypair so the RS256 path is really
tested (not mocked out).

Run: uv run --no-project --with pytest --with boto3 --with cryptography pytest metering/tests/test_interceptor.py -q
"""

import base64
import importlib.util
import json
import os
import pathlib
import sys
import time
from unittest import mock

import pytest

HERE = pathlib.Path(__file__).resolve().parent

# ── local RSA keypair + JWKS for real RS256 verification ────────────────────
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUB = KEY.public_key().public_numbers()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _int_b64url(i: int, length: int | None = None) -> str:
    b = i.to_bytes(length or (i.bit_length() + 7) // 8, "big")
    return _b64url(b)


JWKS = {"keys": [{"kid": "test-kid", "kty": "RSA", "alg": "RS256",
                  "n": _int_b64url(PUB.n), "e": _int_b64url(PUB.e)}]}


def mint_jwt(sub="user-1", groups=("engineering",), exp_delta=3600, kid="test-kid"):
    header = {"alg": "RS256", "kid": kid}
    claims = {"sub": sub, "cognito:groups": list(groups),
              "exp": int(time.time()) + exp_delta, "token_use": "access"}
    signing = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    sig = KEY.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing}.{_b64url(sig)}"


# ── in-memory DynamoDB double (only the ops the interceptor uses) ───────────
class FakeDdb:
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.items: dict = {}
        self.fail = False

    def _k(self, key):
        return (key["pk"]["S"], key["sk"]["S"])

    def batch_get_item(self, RequestItems):
        if self.fail:
            raise RuntimeError("store down")
        (table, spec), = RequestItems.items()
        found = [self.items[self._k(k)] for k in spec["Keys"] if self._k(k) in self.items]
        return {"Responses": {table: found}}

    def put_item(self, TableName, Item, ConditionExpression=None):
        if self.fail:
            raise RuntimeError("store down")
        k = self._k(Item)
        if ConditionExpression and k in self.items:
            raise self.exceptions.ConditionalCheckFailedException()
        self.items[k] = Item
        return {}

    def update_item(self, TableName, Key, **kw):
        if self.fail:
            raise RuntimeError("store down")
        k = self._k(Key)
        item = self.items.setdefault(k, dict(Key))
        vals = kw.get("ExpressionAttributeValues", {})
        expr = kw.get("UpdateExpression", "")
        # minimal ADD handling for est_usd / n
        if "est_usd" in expr and ":e" in vals:
            cur = float(item.get("est_usd", {"N": "0"})["N"])
            item["est_usd"] = {"N": str(cur + float(vals[":e"]["N"]))}
        if "ADD n" in expr and ":one" in vals:
            cur = int(item.get("n", {"N": "0"})["N"])
            item["n"] = {"N": str(cur + 1)}
        return {}


# ── module load with env + stubs ────────────────────────────────────────────
def load_interceptor(fake_ddb, enforce="ENFORCE"):
    env = {
        "MODEL_CAPS": json.dumps({"chat_completions": ["qwen.qwen3-32b"], "messages": ["anthropic.claude-haiku-4-5"]}),
        "TABLE": "metering",
        "JWKS_URL": "https://example.invalid/jwks.json",
        "ENFORCE_MODE": enforce,
        "MAX_TOKENS_CLAMP": "8192",
        "RPM_LIMIT_DEFAULT": "30",
        "GRACE_REQUESTS": "2",
        "HARD_DEFAULT_USD": "5",
        "PROJECT_MAP": json.dumps({"engineering": "proj_eng", "*": "proj_default"}),
    }
    os.environ.pop("PRICE_MAP", None)  # removed: estimates read the DynamoDB catalog
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "metering_interceptor", HERE.parent.parent / "gateway" / "metering-interceptor" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    with mock.patch("boto3.client") as bc:
        bc.side_effect = lambda svc, **kw: fake_ddb if svc == "dynamodb" else mock.MagicMock()
        sys.modules["metering_interceptor"] = mod
        spec.loader.exec_module(mod)
    mod._jwks["keys"] = {k["kid"]: k for k in JWKS["keys"]}
    mod._jwks["fetched"] = time.time()
    mod._fetch_jwks = lambda: None  # no network in tests
    return mod


def gw_event(path, body: dict | None, token: str | None, extra_headers=None):
    """The exact interceptor event shape observed live (spike S1)."""
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return {
        "interceptorInputVersion": "1.0",
        "http": {"gatewayRequest": {
            "path": path,
            "httpMethod": "POST",
            "headers": headers,
            "body": base64.b64encode(json.dumps(body).encode()).decode() if body is not None else None,
        }, "gatewayResponse": None},
    }


CHAT_BODY = {"model": "bedrock/qwen.qwen3-32b",
             "messages": [{"role": "user", "content": "hi"}],
             "stream": True}


def seed_pricing(ddb, model="qwen.qwen3-32b", input_1m="0.15", output_1m="0.6"):
    """A PUBLISHED catalog row in the current contract (per-1M rates grid)."""
    ddb.items[(f"PRICING#{model}", "PUBLISHED")] = {
        "pk": {"S": f"PRICING#{model}"}, "sk": {"S": "PUBLISHED"},
        "model_id": {"S": model}, "_UNIT": {"S": "USD/1M-tokens"},
        "offer_version": {"S": "20260728133434"},
        "rates": {"M": {"in_region": {"M": {"standard": {"M": {"default": {"M": {
            "input": {"N": input_1m}, "output": {"N": output_1m}}}}}}}}},
    }


def _resp_body(result):
    raw = result["http"]["transformedGatewayResponse"]["body"]
    return json.loads(base64.b64decode(raw))


def _req_body(result):
    raw = result["http"]["transformedGatewayRequest"]["body"]
    return json.loads(base64.b64decode(raw))


# ── tests ────────────────────────────────────────────────────────────────────

def test_models_listing_still_capability_filtered_and_never_blocked():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event("/v1/models", None, mint_jwt()), None)
    body = _resp_body(r)
    assert r["http"]["transformedGatewayResponse"]["statusCode"] == 200
    assert body["data"][0]["id"] == "bedrock/qwen.qwen3-32b"


def test_under_quota_forwards_with_mutations_and_floor_debit():
    ddb = FakeDdb()
    seed_pricing(ddb)
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt()), None)
    body = _req_body(r)
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 8192
    # project header injected per group map
    assert r["http"]["transformedGatewayRequest"]["headers"]["OpenAI-Project"] == "proj_eng"
    # floor debit estimate written OPEN, priced from the CATALOG row and keyed
    # by the parsed model id (same key the debit Lambda settles under)
    ests = [v for (pk, sk), v in ddb.items.items() if pk.startswith("EST#")]
    assert len(ests) == 1 and ests[0]["state"]["S"] == "OPEN"
    assert ests[0]["model"]["S"] == "qwen.qwen3-32b"
    body_bytes = len(json.dumps(CHAT_BODY).encode())
    expected = round((body_bytes // 4) * 0.15e-06 + 8192 * 0.6e-06, 8)
    assert abs(float(ests[0]["usd"]["N"]) - expected) < 1e-12


def test_unresolvable_model_estimates_zero_and_admits():
    """No catalog row and no hardcoded fallback rates (Req 8.5): the request
    is admitted with a $0 floor estimate — unpriced is a settle-path signal."""
    ddb = FakeDdb()  # no PRICING rows seeded
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt()), None)
    assert "transformedGatewayRequest" in r["http"]
    ests = [v for (pk, sk), v in ddb.items.items() if pk.startswith("EST#")]
    assert len(ests) == 1 and float(ests[0]["usd"]["N"]) == 0.0


def test_estimate_derives_routing_and_stores_parsed_key():
    """A global.-prefixed invocation estimates at the global rate and the EST
    row carries the catalog key so settle-time matching works (Req 7.1, 8.4)."""
    ddb = FakeDdb()
    ddb.items[("PRICING#anthropic.claude-haiku-4-5", "PUBLISHED")] = {
        "pk": {"S": "PRICING#anthropic.claude-haiku-4-5"}, "sk": {"S": "PUBLISHED"},
        "_UNIT": {"S": "USD/1M-tokens"},
        "rates": {"M": {
            "in_region": {"M": {"standard": {"M": {"default": {"M": {
                "input": {"N": "1.1"}, "output": {"N": "5.5"}}}}}}},
            "global": {"M": {"standard": {"M": {"default": {"M": {
                "input": {"N": "1"}, "output": {"N": "5"}}}}}}},
        }},
    }
    mod = load_interceptor(ddb)
    body = {"model": "bedrock/global.anthropic.claude-haiku-4-5", "max_tokens": 1000,
            "messages": [{"role": "user", "content": "hi"}]}
    mod.lambda_handler(gw_event("/v1/messages", body, mint_jwt()), None)
    ests = [v for (pk, sk), v in ddb.items.items() if pk.startswith("EST#")]
    assert len(ests) == 1
    assert ests[0]["model"]["S"] == "anthropic.claude-haiku-4-5"  # prefix peeled
    body_bytes = len(json.dumps(body).encode())
    expected = round((body_bytes // 4) * 1e-06 + 1000 * 5e-06, 8)  # global rates
    assert abs(float(ests[0]["usd"]["N"]) - expected) < 1e-12


def test_messages_lane_gets_workspace_header():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event(
        "/v1/messages",
        {"model": "bedrock/anthropic.claude-haiku-4-5", "max_tokens": 100,
         "messages": [{"role": "user", "content": "hi"}]},
        mint_jwt()), None)
    assert r["http"]["transformedGatewayRequest"]["headers"]["anthropic-workspace-id"] == "proj_eng"


def test_over_hard_limit_429_with_openai_error_shape():
    ddb = FakeDdb()
    window = time.strftime("%Y-%m", time.gmtime())
    ddb.items[(f"USE#user-1#{window}", "COUNTER")] = {
        "pk": {"S": f"USE#user-1#{window}"}, "sk": {"S": "COUNTER"},
        "used_usd": {"N": "6.0"}, "est_usd": {"N": "0"},
    }
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt()), None)
    sc = r["http"]["transformedGatewayResponse"]
    assert sc["statusCode"] == 429
    assert _resp_body(r)["error"]["code"] == "quota_exceeded"


def test_observe_mode_logs_but_forwards_over_limit():
    ddb = FakeDdb()
    window = time.strftime("%Y-%m", time.gmtime())
    ddb.items[(f"USE#user-1#{window}", "COUNTER")] = {
        "pk": {"S": f"USE#user-1#{window}"}, "sk": {"S": "COUNTER"},
        "used_usd": {"N": "6.0"},
    }
    mod = load_interceptor(ddb, enforce="OBSERVE")
    r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt()), None)
    assert "transformedGatewayRequest" in r["http"]


def test_rpm_exhausted_429_with_retry_after():
    ddb = FakeDdb()
    minute = time.strftime("%Y%m%d%H%M", time.gmtime())
    ddb.items[(f"RPM#user-1#{minute}", "RPM")] = {
        "pk": {"S": f"RPM#user-1#{minute}"}, "sk": {"S": "RPM"}, "n": {"N": "30"},
    }
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt()), None)
    sc = r["http"]["transformedGatewayResponse"]
    assert sc["statusCode"] == 429
    assert sc["headers"]["Retry-After"] == "60"
    assert _resp_body(r)["error"]["code"] == "rate_limit_exceeded"


def test_store_down_fails_open_within_grace_then_closes():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    ddb.fail = True
    tok = mint_jwt()
    ev = gw_event("/v1/chat/completions", CHAT_BODY, tok)
    r1 = mod.lambda_handler(ev, None)
    r2 = mod.lambda_handler(ev, None)
    assert "transformedGatewayRequest" in r1["http"] and "transformedGatewayRequest" in r2["http"]
    r3 = mod.lambda_handler(ev, None)  # grace (2) exhausted
    assert r3["http"]["transformedGatewayResponse"]["statusCode"] == 429


def test_gateway_retry_duplicate_admission_is_noop():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    ev = gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt())
    mod.lambda_handler(ev, None)
    mod.lambda_handler(ev, None)  # same body, same minute → same est key
    ests = [v for (pk, sk), v in ddb.items.items() if pk.startswith("EST#")]
    assert len(ests) == 1
    window = time.strftime("%Y-%m", time.gmtime())
    counter = ddb.items[(f"USE#user-1#{window}", "COUNTER")]
    # est_usd added exactly once
    est_row_usd = float(ests[0]["usd"]["N"])
    assert abs(float(counter["est_usd"]["N"]) - est_row_usd) < 1e-9


def test_bad_signature_forwards_without_enforcement():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    tok = mint_jwt()
    tampered = tok[:-6] + "AAAAAA"
    r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, tampered), None)
    assert "transformedGatewayRequest" in r["http"]
    assert not [1 for (pk, _), _v in ddb.items.items() if pk.startswith("EST#")]


def test_crash_in_handler_returns_passthrough_not_exception():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    with mock.patch.object(mod, "_handle", side_effect=RuntimeError("boom")):
        r = mod.lambda_handler(gw_event("/v1/chat/completions", CHAT_BODY, mint_jwt()), None)
    assert "transformedGatewayRequest" in r["http"] or r["http"] == {}


def test_non_inference_paths_pass_through():
    ddb = FakeDdb()
    mod = load_interceptor(ddb)
    r = mod.lambda_handler(gw_event("/v1/embeddings", {"model": "x", "input": "y"}, mint_jwt()), None)
    assert "transformedGatewayRequest" in r["http"]
    assert not [1 for (pk, _), _v in ddb.items.items() if pk.startswith("EST#")]
