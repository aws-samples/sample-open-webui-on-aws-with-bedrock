"""HTTPv2 Function URL entry point: runtime.handler.handler (Python 3.12 ARM64).

POST /search accepts bearer authentication and JSON {query, count}; positive
counts cap at AWS's 25 and queries must contain 1..200 characters, unchanged.
SERVICE_SECRET_ARN holds a server-generated raw 64-character SecretString.
One secret is cached for at most 60 monotonic seconds; rotation may therefore
accept the old key for up to 60 seconds. Failed refreshes never use stale keys.
GATEWAY_URL, SEARCH_REGION and optional GATEWAY_TARGET_NAME (web-search-tool)
configure the IAM-signed managed connector. Dependencies: boto3/httpx/jsonschema.
Deployment must set a 30-second Lambda timeout; all upstream work shares 20s.
OWUI's pinned external provider swallows non-2xx: request logs distinguish
validation, authentication, unavailable, malformed, throttled, and timeout cases.
"""

import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from threading import Lock

import boto3
from botocore.config import Config

from .gateway import (
    GatewayAuthError, GatewayClient, GatewayProtocolError, GatewayRateLimitError,
    GatewayServiceError, GatewayTimeoutError,
)
from .search import SearchContractError, validate_search_input

MAX_REQUEST_BYTES = 8192
TOTAL_UPSTREAM_SECONDS = 20
SECRET_TTL_SECONDS = 60
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
_secret_cache = None
_secret_lock = Lock()


class SecretUnavailable(RuntimeError):
    """Secret lookup or configuration failed without exposing AWS details."""


def _get_secret() -> str:
    global _secret_cache
    try:
        identity = (os.environ["SERVICE_SECRET_ARN"], os.environ["SEARCH_REGION"])
        with _secret_lock:
            now = time.monotonic()
            if _secret_cache is not None:
                cached_identity, expiry, secret = _secret_cache
                if cached_identity == identity and now < expiry:
                    return secret
            _secret_cache = None
            client = boto3.client("secretsmanager", region_name=identity[1], config=Config(
                connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 1}))
            try:
                secret = client.get_secret_value(SecretId=identity[0])["SecretString"]
            finally:
                client.close()
            if not isinstance(secret, str) or len(secret) != 64 or any(not 33 <= ord(character) <= 126 for character in secret):
                raise ValueError("invalid secret")
            _secret_cache = (identity, now + SECRET_TTL_SECONDS, secret)
            return secret
    except Exception:
        raise SecretUnavailable("Service authentication unavailable") from None


def _token(event: dict) -> str | None:
    headers = event.get("headers", {})
    if not isinstance(headers, dict):
        return None
    values = [value for name, value in headers.items() if isinstance(name, str) and name.lower() == "authorization"]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    parts = values[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) != 64:
        return None
    if any(not 33 <= ord(character) <= 126 for character in parts[1]):
        return None
    return parts[1]


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise SearchContractError("duplicate JSON field")
        result[name] = value
    return result


def _input(event: dict) -> dict:
    try:
        body = event.get("body")
        encoded = event.get("isBase64Encoded", False)
        if not isinstance(body, str) or type(encoded) is not bool:
            raise ValueError()
        if len(body) > (4 * ((MAX_REQUEST_BYTES + 2) // 3) if encoded else MAX_REQUEST_BYTES):
            raise ValueError()
        raw = base64.b64decode(body, validate=True) if encoded else body.encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) != {"query", "count"}:
            raise ValueError()
        count = data["count"]
        if type(count) is not int or count < 1:
            raise ValueError()
        return validate_search_input(data["query"], min(count, 25))
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise SearchContractError("Invalid search request") from None


async def _dispatch(event: dict, token: str):
    secret = await asyncio.to_thread(_get_secret)
    if not hmac.compare_digest(token, secret):
        return 401, {"error": "Unauthorized"}, "unauthorized"
    arguments = _input(event)
    try:
        gateway = GatewayClient(os.environ["GATEWAY_URL"], os.environ["SEARCH_REGION"],
                                os.environ.get("GATEWAY_TARGET_NAME", "web-search-tool"),
                                total_timeout=TOTAL_UPSTREAM_SECONDS)
    except (KeyError, GatewayProtocolError):
        return 503, {"error": "Search unavailable"}, "configuration_error"
    response = await gateway.search(arguments["query"], arguments["maxResults"])
    records = [asdict(record) for record in response.records[:arguments["maxResults"]]]
    return 200, records, "success" if records else "empty"


def _run(event, token):
    """Close without waiting for timed-out credential/secret worker threads."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(_dispatch(event, token), TOTAL_UPSTREAM_SECONDS))
    finally:
        loop.close()


def handler(event, context):
    """Never trust caller headers for AWS credentials, gateway routing or identity."""
    started = time.monotonic()
    status, body, outcome = 400, {"error": "Invalid request"}, "validation_error"
    try:
        if isinstance(event, dict) and event.get("version") == "2.0":
            request_context = event.get("requestContext")
            http = request_context.get("http") if isinstance(request_context, dict) else None
            if (not isinstance(http, dict) or not isinstance(http.get("method"), str)
                    or not isinstance(event.get("rawPath"), str)):
                raise SearchContractError("Invalid HTTP event")
            if event.get("rawPath") != "/search":
                status, body, outcome = 404, {"error": "Not found"}, "not_found"
            elif http.get("method") != "POST":
                status, body, outcome = 405, {"error": "Method not allowed"}, "method_not_allowed"
            else:
                token = _token(event)
                if token is None:
                    status, body, outcome = 401, {"error": "Unauthorized"}, "unauthorized"
                else:
                    status, body, outcome = _run(event, token)
    except SearchContractError:
        status, body, outcome = 400, {"error": "Invalid search request"}, "validation_error"
    except (TimeoutError, GatewayTimeoutError):
        status, body, outcome = 504, {"error": "Search timed out"}, "upstream_timeout"
    except SecretUnavailable:
        status, body, outcome = 503, {"error": "Search unavailable"}, "secret_unavailable"
    except GatewayRateLimitError:
        status, body, outcome = 503, {"error": "Search unavailable"}, "upstream_throttled"
    except GatewayAuthError:
        status, body, outcome = 502, {"error": "Search upstream failed"}, "upstream_auth_error"
    except GatewayProtocolError:
        status, body, outcome = 502, {"error": "Search upstream failed"}, "upstream_protocol_error"
    except GatewayServiceError:
        status, body, outcome = 502, {"error": "Search upstream failed"}, "upstream_service_error"
    except Exception:
        status, body, outcome = 503, {"error": "Search unavailable"}, "internal_error"
    LOGGER.info(json.dumps({"event": "search_request", "request_id": uuid.uuid4().hex,
                            "status": status, "outcome": outcome,
                            "duration_ms": round((time.monotonic() - started) * 1000)}))
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status == 405:
        headers["Allow"] = "POST"
    return {"statusCode": status, "headers": headers, "isBase64Encoded": False,
            "body": json.dumps(body, ensure_ascii=True, separators=(",", ":"))}
