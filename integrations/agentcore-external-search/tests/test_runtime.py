"""Native external-provider contract tests; no live AWS calls."""

import asyncio
import base64
import importlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from botocore.credentials import ReadOnlyCredentials

from runtime.gateway import (
    GatewayAuthError, GatewayClient, GatewayProtocolError, GatewayRateLimitError,
    GatewayServiceError, GatewayTimeoutError, PROTOCOL_VERSION,
)
from runtime.search import SearchContractError, SearchRecord, SearchResponse, parse_search_response

runtime = importlib.import_module("runtime.handler")
KEY = "k" * 64
URL = "https://search-abcdefghij.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def event(query="AWS docs", count=5, **changes):
    return {"version": "2.0", "rawPath": "/search", "requestContext": {"http": {"method": "POST"}},
            "headers": {"authorization": "Bearer " + KEY},
            "body": json.dumps({"query": query, "count": count}), "isBase64Encoded": False, **changes}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", URL)
    monkeypatch.setenv("SEARCH_REGION", "us-east-1")
    monkeypatch.setenv("SERVICE_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:search")
    monkeypatch.delenv("GATEWAY_TARGET_NAME", raising=False)
    monkeypatch.setattr(runtime, "_get_secret", lambda: KEY)
    calls = []

    class Gateway:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        async def search(self, query, count):
            calls.append((query, count))
            return SearchResponse(tuple(SearchRecord(f"https://example.com/{index}", f"Title {index}", "Snippet")
                                        for index in range(count)), 0)

    monkeypatch.setattr(runtime, "GatewayClient", Gateway)
    return calls


@pytest.mark.parametrize("count,expected", [(1, 1), (5, 5), (25, 25), (26, 25), (1000, 25)])
def test_native_count_contract_and_config(service, count, expected):
    response = runtime.handler(event(count=count), None)
    assert response["statusCode"] == 200
    records = json.loads(response["body"])
    assert len(records) == expected
    assert records[0] == {"link": "https://example.com/0", "title": "Title 0", "snippet": "Snippet"}
    assert service == [((URL, "us-east-1", "web-search-tool"), {"total_timeout": 20}), ("AWS docs", expected)]
    assert response["headers"]["Cache-Control"] == "no-store"


@pytest.mark.parametrize("query", ["a", "a" * 200, "é" * 200, "  query  "])
def test_query_boundaries_are_not_rewritten(service, query):
    assert runtime.handler(event(query=query), None)["statusCode"] == 200
    assert service[-1] == (query, 5)


@pytest.mark.parametrize("query", ["", "x" * 201, None, 123, True, [], {}])
def test_query_validation(service, query):
    assert runtime.handler(event(query=query), None)["statusCode"] == 400
    assert service == []


@pytest.mark.parametrize("count", [0, -1, 1.0, True, False, "5", None, [], {}])
def test_positive_integer_count_required(service, count):
    assert runtime.handler(event(count=count), None)["statusCode"] == 400
    assert service == []


@pytest.mark.parametrize("body", [None, {}, "", "{", "[]", "null", '{"query":"hi"}', '{"count":5}',
    '{"query":"hi","count":5,"user_id":"attacker"}', '{"query":"hi","count":5,"count":25}',
    '{"query":"hi","count":NaN}', '{"query":"hi","count":Infinity}', '"' + "[" * 9000,
    "[" * 2000 + "]" * 2000, "\ud800"])
def test_bad_bodies_never_invoke_gateway(service, body):
    assert runtime.handler(event(body=body), None)["statusCode"] == 400
    assert service == []


@pytest.mark.parametrize("encoded", [False, True])
def test_exact_decoded_request_byte_limit(service, encoded):
    for size, status in [(8192, 200), (8193, 400)]:
        body = event()["body"].encode()
        body += b" " * (size - len(body))
        wire_body = base64.b64encode(body).decode() if encoded else body.decode()
        assert runtime.handler(event(body=wire_body, isBase64Encoded=encoded), None)["statusCode"] == status


def test_request_limit_is_bytes_not_characters(service):
    body = json.dumps({"query": "é" * 200, "count": 5}, ensure_ascii=False)
    body += " " * (8192 - len(body))
    assert runtime.handler(event(body=body), None)["statusCode"] == 400
    assert service == []


@pytest.mark.parametrize("body,flag", [("!invalid!", True), ("e30=\n", True), ("/w==", True), ("e30=", "true")])
def test_malformed_base64_and_utf8(service, body, flag):
    assert runtime.handler(event(body=body, isBase64Encoded=flag), None)["statusCode"] == 400


@pytest.mark.parametrize("headers", [{}, None, {"Authorization": "Basic " + KEY},
    {"authorization": "Bearer wrong"}, {"authorization": "Bearer " + "x" * 64},
    {"authorization": "Bearer " + KEY + ",Bearer " + KEY},
    {"authorization": "Bearer " + "é" * 64}, {"authorization": ["Bearer " + KEY]},
    {"authorization": "Bearer " + KEY, "Authorization": "Bearer " + KEY},
    {"x-api-key": KEY}, {"authorization": "Bearer  " + KEY}])
def test_unauthorized_never_searches(service, headers):
    response = runtime.handler(event(headers=headers), None)
    assert response["statusCode"] == 401
    assert response["headers"]["WWW-Authenticate"] == "Bearer"
    assert service == []


def test_header_and_bearer_scheme_are_case_insensitive(service):
    assert runtime.handler(event(headers={"AUTHORIZATION": "bEaReR " + KEY}), None)["statusCode"] == 200


@pytest.mark.parametrize("changes,status", [({"rawPath": "/"}, 404), ({"rawPath": "/search/"}, 404),
    ({"requestContext": {"http": {"method": "GET"}}}, 405),
    ({"requestContext": {"http": {"method": "OPTIONS"}}}, 405), ({"version": "1.0"}, 400)])
def test_routing_without_secret_lookup(service, monkeypatch, changes, status):
    monkeypatch.setattr(runtime, "_get_secret", lambda: pytest.fail("routing accessed secret"))
    response = runtime.handler(event(**changes), None)
    assert response["statusCode"] == status
    if status == 405:
        assert response["headers"]["Allow"] == "POST"
    assert service == []


@pytest.mark.parametrize("request_context", [None, [], {}, {"http": None}, {"http": {}}, {"http": {"method": 5}}])
def test_malformed_http_event_is_validation_error(service, request_context):
    assert runtime.handler(event(requestContext=request_context), None)["statusCode"] == 400
    assert service == []


@pytest.mark.parametrize("setting,value", [("GATEWAY_URL", None), ("GATEWAY_URL", "http://evil.invalid/mcp"),
    ("SEARCH_REGION", None), ("GATEWAY_TARGET_NAME", "evil___Other")])
def test_missing_or_malformed_gateway_configuration(service, monkeypatch, setting, value):
    monkeypatch.setattr(runtime, "GatewayClient", GatewayClient)
    if value is None:
        monkeypatch.delenv(setting)
    else:
        monkeypatch.setenv(setting, value)
    assert runtime.handler(event(), None)["statusCode"] == 503
    assert service == []


@pytest.mark.parametrize("error,status,outcome", [(GatewayAuthError, 502, "upstream_auth_error"),
    (GatewayProtocolError, 502, "upstream_protocol_error"), (GatewayServiceError, 502, "upstream_service_error"),
    (GatewayRateLimitError, 503, "upstream_throttled"), (GatewayTimeoutError, 504, "upstream_timeout"),
    (runtime.SecretUnavailable, 503, "secret_unavailable"), (RuntimeError, 503, "internal_error")])
def test_upstream_status_and_sanitized_logs(service, monkeypatch, caplog, error, status, outcome):
    async def fail(*args):
        raise error("private query token upstream body")

    monkeypatch.setattr(runtime.GatewayClient, "search", fail)
    response = runtime.handler(event(query="private query"), SimpleNamespace(aws_request_id="caller-controlled"))
    assert response["statusCode"] == status
    assert len(caplog.records) == 1
    logged = json.loads(caplog.records[0].message)
    assert logged["outcome"] == outcome and logged["status"] == status
    assert set(logged) == {"event", "request_id", "status", "outcome", "duration_ms"}
    assert all(value not in caplog.text + response["body"] for value in ["private query", "token", KEY, "caller-controlled"])


def test_real_empty_is_success_and_logged_distinctly(service, monkeypatch, caplog):
    async def empty(*args):
        return SearchResponse((), 0)

    monkeypatch.setattr(runtime.GatewayClient, "search", empty)
    response = runtime.handler(event(), None)
    assert response["statusCode"] == 200 and response["body"] == "[]"
    assert json.loads(caplog.records[0].message)["outcome"] == "empty"


def test_total_deadline_covers_secret_and_gateway(service, monkeypatch):
    monkeypatch.setattr(runtime, "TOTAL_UPSTREAM_SECONDS", 0.10)

    def slow_secret():
        time.sleep(0.06)
        return KEY

    async def slow_search(*args):
        await asyncio.sleep(0.06)
        return SearchResponse((), 0)

    monkeypatch.setattr(runtime, "_get_secret", slow_secret)
    monkeypatch.setattr(runtime.GatewayClient, "search", slow_search)
    assert runtime.handler(event(), None)["statusCode"] == 504


def test_timeout_does_not_wait_for_blocking_secret_thread(service, monkeypatch):
    monkeypatch.setattr(runtime, "TOTAL_UPSTREAM_SECONDS", 0.02)

    def slow_secret():
        time.sleep(0.3)
        return KEY

    monkeypatch.setattr(runtime, "_get_secret", slow_secret)
    started = time.monotonic()
    assert runtime.handler(event(), None)["statusCode"] == 504
    assert time.monotonic() - started < 0.2
    assert service == []


def envelope(result):
    return {"jsonrpc": "2.0", "id": "fixture", "result": result}


def text_block(payload):
    return {"type": "text", "text": json.dumps(payload)}


@pytest.mark.parametrize("records", [[], [{"url": "https://example.com", "text": "Grounded text"}]])
@pytest.mark.parametrize("structured", [False, True])
def test_matching_search_payloads_supported(records, structured):
    payload = {"results": records}
    tool = {"content": [text_block(payload), {"type": "text", "text": "Attribution note"}, text_block(payload)]}
    if structured:
        tool["structuredContent"] = payload
    assert len(parse_search_response(envelope(tool)).records) == len(records)


@pytest.mark.parametrize("structured", [None, [], {}, {"results": None}, {"results": {}}, {"results": "private"}])
def test_malformed_structured_content_never_falls_back(structured):
    tool = {"structuredContent": structured, "content": [text_block({"results": []})]}
    with pytest.raises(SearchContractError, match="^malformed structured search payload$"):
        parse_search_response(envelope(tool))


@pytest.mark.parametrize("reverse", [False, True])
def test_structured_text_disagreement_including_empty_is_rejected(reverse):
    empty = {"results": []}
    nonempty = {"results": [{"url": "https://example.com", "text": "private"}]}
    structured, text = (nonempty, empty) if reverse else (empty, nonempty)
    tool = {"structuredContent": structured, "content": [text_block(structured), text_block(text)]}
    with pytest.raises(SearchContractError, match="^ambiguous search payloads$"):
        parse_search_response(envelope(tool))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("ambiguous", [
    '{"results":[{"text":"private"}],"results":[]}',
    '{"results":[],"\\u0072esults":[]}',
    '{"result":"private","result":"overwritten","results":[]}',
    '{"results":[{"url":"https://example.com","text":"private","text":"overwritten"}]}',
])
def test_duplicate_text_keys_never_skipped_for_valid_payload(ambiguous, reverse, structured):
    blocks = [text_block({"results": []}), {"type": "text", "text": ambiguous}]
    tool = {"content": blocks[::-1] if reverse else blocks}
    if structured:
        tool["structuredContent"] = {"results": []}
    with pytest.raises(SearchContractError, match="^duplicate JSON key$"):
        parse_search_response(envelope(tool))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("malformed", [None, {}, "private", 5])
def test_recognized_malformed_text_payload_never_skipped(reverse, malformed):
    blocks = [text_block({"results": []}), text_block({"results": malformed})]
    with pytest.raises(SearchContractError, match="^malformed text search payload$"):
        parse_search_response(envelope({"content": blocks[::-1] if reverse else blocks}))


@pytest.mark.parametrize("tool", [
    {"structuredContent": {"results": []}, "content": [text_block({"results": [{"text": "private", "url": "https://example.com"}]})]},
    {"structuredContent": {"results": [{"text": "private", "url": "https://example.com"}]}, "content": [text_block({"results": []})]},
    {"structuredContent": None, "content": [text_block({"results": []})]},
    {"structuredContent": {}, "content": [text_block({"results": []})]},
    {"structuredContent": {"results": []}, "content": [text_block({"results": None})]},
    {"content": [{"type": "text", "text": '{"results":[{"text":"private"}],"results":[]}'}, text_block({"results": []})]},
])
def test_ambiguous_search_response_returns_opaque_502(service, monkeypatch, caplog, tool):
    def upstream(request):
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION}
        elif method == "tools/list":
            result = {"tools": [{"name": "web-search-tool___WebSearch", "inputSchema": {"type": "object",
                "properties": {"query": {"type": "string"}, "maxResults": {"type": "integer"}}, "required": ["query"]}}]}
        else:
            result = tool
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def gateway(*args, **kwargs):
        return GatewayClient(*args, **kwargs, credentials=ReadOnlyCredentials("role-key", "role-secret", "role-token"),
                             transport=httpx.MockTransport(upstream))

    monkeypatch.setattr(runtime, "GatewayClient", gateway)
    response = runtime.handler(event(), None)
    assert response["statusCode"] == 502
    assert json.loads(response["body"]) == {"error": "Search upstream failed"}
    assert json.loads(caplog.records[0].message)["outcome"] == "upstream_protocol_error"
    assert "private" not in response["body"] + caplog.text


@pytest.mark.parametrize("wire_format", ["structured", "text"])
def test_aws_native_payload_and_missing_metadata(wire_format):
    payload = {"results": [{"url": "https://example.com", "text": "Grounded text"},
                           {"text": "No citation URL"},
                           {"url": "https://example.org", "title": "Title", "text": "Other text", "publishedDate": "2026-09-16"}]}
    tool = {"structuredContent": payload} if wire_format == "structured" else {"content": [{"type": "text", "text": json.dumps(payload)}]}
    result = parse_search_response(envelope(tool))
    assert result.records == (SearchRecord("https://example.com", "https://example.com", "Grounded text"),
                              SearchRecord("https://example.org", "Title", "Other text"))
    assert result.omitted_count == 1


@pytest.mark.parametrize("tool", [{}, {"structuredContent": {}}, {"structuredContent": {"results": None}},
    {"structuredContent": {"results": [{}]}}, {"structuredContent": {"results": [None]}},
    {"structuredContent": {"results": [{"text": "No URL"}]}},
    {"structuredContent": {"results": [{"text": "text", "url": "https://example.com", "title": None}]}},
    {"content": [{"type": "text", "text": "not json"}]}, {"isError": "false"},
    {"content": [{"type": "text", "text": '{"results":[]}'},
                 {"type": "text", "text": '{"results":[{"text":"conflict"}]}'}]}])
def test_malformed_payload_is_not_empty(tool):
    with pytest.raises(SearchContractError):
        parse_search_response(envelope(tool))


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.169.254/latest", "http://[::1]", "file:///tmp/a",
    "javascript:alert(1)", "https://user:pass@example.com", "http://host.local", "https://example.com/\nsecret"])
def test_unsafe_citations_are_not_returned(url):
    with pytest.raises(SearchContractError):
        parse_search_response(envelope({"structuredContent": {"results": [{"text": "text", "url": url}]}}))


@pytest.mark.parametrize("count", [5, 25])
def test_handler_to_signed_mcp_native_contract_ignores_caller_headers(service, monkeypatch, count):
    requests = []

    def upstream(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION}
        elif method == "tools/list":
            result = {"tools": [{"name": "web-search-tool___WebSearch", "inputSchema": {"type": "object",
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200},
                               "maxResults": {"type": "integer", "minimum": 1, "maximum": 25}}, "required": ["query"]}}]}
        else:
            assert payload["params"] == {"name": "web-search-tool___WebSearch", "arguments": {"query": "AWS docs", "maxResults": count}}
            result = {"content": [{"type": "text", "text": json.dumps({"results": [
                {"url": f"https://example.com/{index}", "text": f"Snippet {index}"} for index in range(count)]})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def gateway(*args, **kwargs):
        return GatewayClient(*args, **kwargs, credentials=ReadOnlyCredentials("role-key", "role-secret", "role-token"),
                             transport=httpx.MockTransport(upstream))

    monkeypatch.setattr(runtime, "GatewayClient", gateway)
    headers = {"authorization": "Bearer " + KEY, "x-amz-security-token": "attacker-token", "x-amz-date": "evil",
        "mcp-session-id": "attacker-session", "mcp-protocol-version": "evil", "host": "evil.invalid",
        "x-forwarded-user": "attacker", "x-openwebui-user-id": "attacker", "x-gateway-url": "https://evil.invalid",
        "x-gateway-target-name": "evil", "cookie": "attacker-cookie", "x-api-key": "attacker-key"}
    response = runtime.handler(event(count=count, headers=headers), None)
    assert response["statusCode"] == 200 and len(json.loads(response["body"])) == count
    assert len(requests) == 4
    for request, payload in requests:
        assert str(request.url) == URL
        assert request.headers["x-amz-security-token"] == "role-token"
        assert "Credential=role-key/" in request.headers["authorization"]
        assert request.headers["mcp-protocol-version"] == PROTOCOL_VERSION
        assert "mcp-session-id" not in request.headers
        assert not any(name in request.headers for name in ("x-openwebui-user-id", "x-forwarded-user", "x-gateway-url", "cookie", "x-api-key"))
        assert KEY not in str(request.headers) and "attacker" not in str(request.headers)


@pytest.fixture
def secret_store(monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET_ARN", "configured-secret-arn")
    monkeypatch.setenv("SEARCH_REGION", "us-east-1")
    monkeypatch.setattr(runtime, "_secret_cache", None)
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    state = {"secret": KEY, "calls": [], "clock": clock, "closed": 0}

    class Secrets:
        def get_secret_value(self, **kwargs):
            state["calls"].append(kwargs)
            if isinstance(state["secret"], Exception):
                raise state["secret"]
            return {"SecretString": state["secret"]}

        def close(self):
            state["closed"] += 1

    def factory(name, *, region_name, config):
        assert name == "secretsmanager" and region_name == "us-east-1"
        assert config.connect_timeout == 2 and config.read_timeout == 2
        assert config.retries == {"total_max_attempts": 1}
        return Secrets()

    monkeypatch.setattr(runtime.boto3, "client", factory)
    return state


def test_secret_cache_ttl_rotation_and_no_stale_fallback(secret_store):
    assert runtime._get_secret() == KEY
    secret_store["secret"] = "n" * 64
    secret_store["clock"][0] = 159.99
    assert runtime._get_secret() == KEY and len(secret_store["calls"]) == 1
    secret_store["clock"][0] = 160
    assert runtime._get_secret() == "n" * 64 and len(secret_store["calls"]) == 2
    secret_store["clock"][0] = 220
    secret_store["secret"] = RuntimeError("private AWS error")
    with pytest.raises(runtime.SecretUnavailable, match="Service authentication unavailable"):
        runtime._get_secret()
    assert runtime._secret_cache is None
    assert secret_store["closed"] == 3


def test_secret_cache_key_includes_arn(secret_store, monkeypatch):
    assert runtime._get_secret() == KEY
    monkeypatch.setenv("SERVICE_SECRET_ARN", "rotated-arn")
    assert runtime._get_secret() == KEY
    assert secret_store["calls"] == [{"SecretId": "configured-secret-arn"}, {"SecretId": "rotated-arn"}]


@pytest.mark.parametrize("secret", [None, "", "short", "x" * 65, " " * 64, "é" * 64, {"api_key": KEY}, RuntimeError("private")])
def test_malformed_or_unavailable_secrets_fail_closed(secret_store, secret):
    secret_store["secret"] = secret
    response = runtime.handler(event(), None)
    assert response["statusCode"] == 503
    assert "private" not in response["body"] and runtime._secret_cache is None
