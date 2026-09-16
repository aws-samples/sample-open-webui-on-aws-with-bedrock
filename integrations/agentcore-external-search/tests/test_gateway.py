"""Mocked documented MCP lifecycle coverage, not live AWS interoperability."""

import asyncio
import copy
import json

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials

from runtime.gateway import (
    GatewayAuthError, GatewayClient, GatewayProtocolError, GatewayRateLimitError,
    GatewayServiceError, GatewayTimeoutError, PROTOCOL_VERSION,
)
from runtime.search import SearchContractError

URL = "https://research-abcdefghij.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
CREDS = ReadOnlyCredentials("synthetic-key", "synthetic-secret", "synthetic-token")
TOOL = {"name": "search___WebSearch", "inputSchema": {"type": "object",
        "properties": {"query": {"type": "string"}, "maxResults": {"type": "integer"}}, "required": ["query"]}}


def reply(payload, result):
    return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


class Server:
    def __init__(self, override=None):
        self.requests, self.override = [], override

    async def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append((request, payload))
        if self.override:
            response = self.override(request, payload)
            if response is not None:
                return response
        if payload["method"] == "notifications/initialized":
            assert "id" not in payload
            return httpx.Response(202)
        results = {
            "initialize": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fixture", "version": "1"}},
            "tools/list": {"tools": [TOOL]},
            "tools/call": {"structuredContent": {"results": [{"text": "grounded", "url": "https://example.com"}]}},
        }
        headers = {"Mcp-Session-Id": "session-fixture"} if payload["method"] == "initialize" else {}
        return httpx.Response(200, json=reply(payload, results[payload["method"]]), headers=headers)


def client(server, **kwargs):
    return GatewayClient(URL, "us-east-1", "search", credentials=CREDS,
                         transport=httpx.MockTransport(server), **kwargs)


def test_lifecycle_signature_session_and_normalized_result():
    server = Server()
    result = asyncio.run(client(server).search("AWS docs", 3))
    assert result.records[0].link == "https://example.com"
    assert [payload["method"] for _, payload in server.requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert server.requests[0][1]["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert server.requests[-1][1]["params"] == {"name": "search___WebSearch", "arguments": {"query": "AWS docs", "maxResults": 3}}
    for index, (request, _) in enumerate(server.requests):
        assert request.headers["mcp-protocol-version"] == PROTOCOL_VERSION
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["x-amz-security-token"] == CREDS.token
        assert request.headers.get("mcp-session-id") == ("session-fixture" if index else None)
        authorization = request.headers["authorization"]
        assert "/us-east-1/bedrock-agentcore/aws4_request" in authorization
        signed = authorization.split("SignedHeaders=", 1)[1].split(",", 1)[0].split(";")
        verification = AWSRequest(method="POST", url=str(request.url), data=request.content,
                                  headers={name: request.headers[name] for name in signed})
        verification.context["timestamp"] = request.headers["x-amz-date"]
        signer = SigV4Auth(CREDS, "bedrock-agentcore", "us-east-1")
        expected = signer.signature(signer.string_to_sign(verification, signer.canonical_request(verification)), verification)
        assert authorization.endswith("Signature=" + expected)


def test_runtime_credentials_refreshed_for_every_signed_request(monkeypatch):
    snapshots = []

    class Provider:
        def get_credentials(self):
            return self

        def get_frozen_credentials(self):
            snapshots.append(len(snapshots))
            return ReadOnlyCredentials(f"key-{len(snapshots)}", "secret", "token")

    def session_factory(*, region_name):
        assert region_name == "us-east-1"
        return Provider()

    monkeypatch.setattr("runtime.gateway.boto3.Session", session_factory)
    server = Server()
    gateway = GatewayClient(URL, "us-east-1", "search", transport=httpx.MockTransport(server))
    asyncio.run(gateway.search("query"))
    assert len(snapshots) == 4
    assert all(f"Credential=key-{index + 1}/" in request.headers["authorization"] for index, (request, _) in enumerate(server.requests))


@pytest.mark.parametrize("url", [URL.replace("https:", "http:"), URL + "?query=secret", URL + "#fragment",
    URL.replace("research-", "user:pass@research-"), URL.replace("us-east-1", "eu-west-1"),
    URL.replace(".com/mcp", ".com.evil.test/mcp"), URL.replace("/mcp", "/other"), URL.replace(".com/", ".com:8443/")])
def test_rejects_unsafe_gateway_urls(url):
    with pytest.raises(GatewayProtocolError):
        GatewayClient(url, "us-east-1", "search", credentials=CREDS)


@pytest.mark.parametrize("status,error", [(401, GatewayAuthError), (403, GatewayAuthError),
    (429, GatewayRateLimitError), (500, GatewayServiceError), (302, GatewayProtocolError)])
def test_http_errors_are_opaque_and_not_retried(status, error):
    server = Server(lambda request, payload: httpx.Response(status, text="secret query raw-token", headers={"Location": "https://evil.test"}))
    with pytest.raises(error) as caught:
        asyncio.run(client(server).search("secret query"))
    assert "secret" not in str(caught.value) and len(server.requests) == 1


def test_call_throttle_is_not_replayed():
    server = Server(lambda request, payload: httpx.Response(429) if payload["method"] == "tools/call" else None)
    with pytest.raises(GatewayRateLimitError):
        asyncio.run(client(server).search("query"))
    assert len(server.requests) == 4


@pytest.mark.parametrize("schema", [None, {"type": "object", "properties": {}, "required": ["query"]},
    {"type": "object", "properties": TOOL["inputSchema"]["properties"], "required": ["query", "secret"]},
    {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}])
def test_discovery_schema_guard_prevents_invocation(schema):
    tool = {**TOOL, "inputSchema": schema}
    server = Server(lambda request, payload: httpx.Response(200, json=reply(payload, {"tools": [tool]})) if payload["method"] == "tools/list" else None)
    with pytest.raises(GatewayProtocolError):
        asyncio.run(client(server).search("query"))
    assert len(server.requests) == 3


def test_exact_target_discovery_with_pagination():
    def override(request, payload):
        if payload["method"] == "tools/list" and "cursor" not in payload["params"]:
            return httpx.Response(200, json=reply(payload, {"tools": [{**TOOL, "name": "other___WebSearch"}], "nextCursor": "next"}))
    server = Server(override)
    asyncio.run(client(server).search("query"))
    assert server.requests[3][1]["params"] == {"cursor": "next"}


def test_cursor_cycle_is_bounded():
    server = Server(lambda request, payload: httpx.Response(200, json=reply(payload, {"tools": [], "nextCursor": "same"})) if payload["method"] == "tools/list" else None)
    with pytest.raises(GatewayProtocolError):
        asyncio.run(client(server).search("query"))
    assert len(server.requests) == 4


@pytest.mark.parametrize("mode", ["wrong-id", "invalid-json", "wrong-version", "rpc-error", "tool-error"])
def test_malformed_or_error_responses_are_opaque(mode):
    def override(request, payload):
        if payload["method"] == "initialize" and mode == "wrong-version":
            return httpx.Response(200, json=reply(payload, {"protocolVersion": "2026-07-28"}))
        if payload["method"] != "tools/call":
            return None
        if mode == "invalid-json":
            return httpx.Response(200, content=b"secret broken JSON", headers={"Content-Type": "application/json"})
        if mode == "rpc-error":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32603, "message": "secret"}})
        message = reply(payload, {"isError": True, "content": [{"type": "text", "text": "secret"}]})
        if mode == "wrong-id":
            message["id"] = "different"
        return httpx.Response(200, json=message)
    server = Server(override)
    error = GatewayServiceError if mode in ("rpc-error", "tool-error") else GatewayProtocolError
    with pytest.raises(error) as caught:
        asyncio.run(client(server).search("query"))
    assert "secret" not in str(caught.value)


def test_sse_multiline_fragmented_result_closes_without_waiting_for_eof():
    closed = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, message):
            self.message = message

        async def __aiter__(self):
            notification = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n'
            frame = b": keepalive\r\n\r\n" + notification + b"\r\n".join(b"data: " + line for line in json.dumps(self.message, indent=2).encode().splitlines()) + b"\r\n\r\n"
            for offset in range(0, len(frame), 7):
                yield frame[offset:offset + 7]
            await asyncio.sleep(10)

        async def aclose(self):
            closed.append(True)

    def override(request, payload):
        if payload["method"] == "tools/call":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Stream(reply(payload, {"structuredContent": {"results": []}})))
    result = asyncio.run(client(Server(override), total_timeout=0.5).search("query"))
    assert result.records == () and closed == [True]


@pytest.mark.parametrize("body", [b'data: {"jsonrpc":"2.0","id":"wrong","result":{}}\n\n', b"data: not-json\n\n", b": heartbeat\n\n"])
def test_sse_invalid_or_uncorrelated_streams(body):
    server = Server(lambda request, payload: httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"}))
    with pytest.raises(GatewayProtocolError):
        asyncio.run(client(server).search("query"))


def test_aggregate_response_byte_limit():
    def override(request, payload):
        if payload["method"] in ("initialize", "tools/list"):
            result = {"protocolVersion": PROTOCOL_VERSION} if payload["method"] == "initialize" else {"tools": [TOOL]}
            return httpx.Response(200, json=reply(payload, {**result, "padding": "x" * 300}))
    server = Server(override)
    with pytest.raises(GatewayProtocolError, match="response limit"):
        asyncio.run(client(server, max_response_bytes=700).search("query"))
    assert len(server.requests) == 3


def test_total_deadline_and_transport_errors_are_opaque():
    async def slow(request):
        await asyncio.sleep(10)
    with pytest.raises(GatewayTimeoutError):
        asyncio.run(client(slow, total_timeout=0.02).search("query"))
    def broken(request):
        raise httpx.ConnectError("secret endpoint detail")
    with pytest.raises(GatewayServiceError) as caught:
        asyncio.run(client(broken).search("query"))
    assert str(caught.value) == "Gateway transport failed"


def test_invalid_query_and_boolean_limit_do_not_send_requests():
    server = Server()
    with pytest.raises(SearchContractError):
        asyncio.run(client(server).search("query", True))
    with pytest.raises(SearchContractError):
        asyncio.run(client(server).search("x" * 201))
    assert server.requests == []


def test_credential_failure_is_opaque_and_precedes_transport(monkeypatch):
    def failed_session(**kwargs):
        raise RuntimeError("secret credential detail")
    monkeypatch.setattr("runtime.gateway.boto3.Session", failed_session)
    server = Server()
    with pytest.raises(GatewayAuthError) as caught:
        asyncio.run(GatewayClient(URL, "us-east-1", "search", transport=httpx.MockTransport(server)).search("query"))
    assert "secret" not in str(caught.value) and server.requests == []


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("fields", [
    '"result":{"isError":true},"result":{"structuredContent":{"results":[]}}',
    '"result":{"isError":true},"\\u0072esult":{"structuredContent":{"results":[]}}',
    '"error":{"code":-32603,"message":"private"},"error":{"code":-32603,"message":"overwritten"}',
    '"result":{"structuredContent":{"results":[{"text":"private"}],"results":[]}}',
    '"result":{"structuredContent":{"results":[{"text":"private","text":"overwritten","url":"https://example.com"}]}}',
])
def test_duplicate_envelope_keys_rejected_in_json_and_sse(sse, fields):
    def override(request, payload):
        if payload["method"] != "tools/call":
            return None
        raw = '{"jsonrpc":"2.0","id":' + json.dumps(payload["id"]) + ',' + fields + '}'
        body = "data: " + raw + "\n\n" if sse else raw
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream" if sse else "application/json"})

    server = Server(override)
    with pytest.raises(GatewayProtocolError) as caught:
        asyncio.run(client(server).search("query"))
    assert str(caught.value) == "Invalid gateway JSON"
    assert len(server.requests) == 4


def test_sse_notification_duplicate_keys_cannot_be_skipped():
    def override(request, payload):
        if payload["method"] != "tools/call":
            return None
        notification = 'data: {"jsonrpc":"2.0","method":"private","method":"notifications/progress"}\n\n'
        result = "data: " + json.dumps(reply(payload, {"structuredContent": {"results": []}})) + "\n\n"
        return httpx.Response(200, content=notification + result, headers={"Content-Type": "text/event-stream"})

    with pytest.raises(GatewayProtocolError, match="^Invalid gateway JSON$"):
        asyncio.run(client(Server(override)).search("query"))


@pytest.mark.parametrize("field,constraint", [
    ("query", {"maxLength": 3}), ("query", {"minLength": 6}),
    ("maxResults", {"maximum": 4}), ("maxResults", {"minimum": 11}),
    ("query", {"enum": ["allowed"]}), ("maxResults", {"enum": [1, 2]}),
    ("query", {"pattern": "^allowed$"}), ("maxResults", {"multipleOf": 3}),
    ("query", {"maxLength": -1}), ("maxResults", {"maximum": "malformed"}),
])
def test_actual_arguments_must_satisfy_advertised_schema(field, constraint):
    tool = copy.deepcopy(TOOL)
    tool["inputSchema"]["properties"][field].update(constraint)
    server = Server(lambda request, payload: httpx.Response(200, json=reply(payload, {"tools": [tool]})) if payload["method"] == "tools/list" else None)
    with pytest.raises(GatewayProtocolError) as caught:
        asyncio.run(client(server).search("query"))
    assert str(caught.value) == "Unsupported Web Search schema or arguments"
    assert [payload["method"] for _, payload in server.requests] == ["initialize", "notifications/initialized", "tools/list"]


def test_valid_tighter_schema_allows_call():
    tool = copy.deepcopy(TOOL)
    tool["inputSchema"]["properties"]["query"].update({"minLength": 5, "maxLength": 5, "enum": ["query"]})
    tool["inputSchema"]["properties"]["maxResults"].update({"minimum": 1, "maximum": 10, "enum": [10]})
    tool["inputSchema"]["additionalProperties"] = False
    server = Server(lambda request, payload: httpx.Response(200, json=reply(payload, {"tools": [tool]})) if payload["method"] == "tools/list" else None)
    result = asyncio.run(client(server).search("query"))
    assert len(result.records) == 1 and server.requests[-1][1]["method"] == "tools/call"


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("location", ["root", "property", "nested-array"])
def test_references_rejected_before_any_schema_resolution(monkeypatch, keyword, location):
    tool = copy.deepcopy(TOOL)
    reference = {keyword: "https://must-not-resolve.invalid/schema"}
    if location == "root":
        tool["inputSchema"].update(reference)
    elif location == "property":
        tool["inputSchema"]["properties"]["query"].update(reference)
    else:
        tool["inputSchema"]["allOf"] = [{"anyOf": [{"properties": {"unused": reference}}]}]
    validation_calls = []
    for method in ("check_schema", "validate"):
        monkeypatch.setattr(f"runtime.gateway.Draft202012Validator.{method}",
                            lambda *args, **kwargs: validation_calls.append(True))
    server = Server(lambda request, payload: httpx.Response(200, json=reply(payload, {"tools": [tool]})) if payload["method"] == "tools/list" else None)
    with pytest.raises(GatewayProtocolError, match="Unsupported Web Search schema or arguments"):
        asyncio.run(client(server).search("query"))
    assert validation_calls == []
    assert len(server.requests) == 3 and all(payload["method"] != "tools/call" for _, payload in server.requests)
