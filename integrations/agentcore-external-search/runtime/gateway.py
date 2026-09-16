"""Bounded MCP 2025-03-26 lifecycle using Lambda-role SigV4 credentials.

Initialize, initialized notification, discovery, and invocation follow the older
MCP lifecycle, not stateless revisions. AWS interoperability is not live-verified.
No retries, persistence, logging, or automatic execution of server requests.
"""

import asyncio
import json
import math
import re
import uuid
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from jsonschema import Draft202012Validator

from .search import (
    SearchContractError, SearchResponse, SearchToolError, parse_search_response,
    unique_json_object, validate_search_input,
)

PROTOCOL_VERSION = "2025-03-26"


class GatewayError(RuntimeError):
    """Opaque gateway failure; no request or response content is included."""


class GatewayAuthError(GatewayError):
    """Credential resolution or gateway authentication/authorization failed."""


class GatewayRateLimitError(GatewayError):
    """Gateway throttled the request; invocation is never automatically retried."""


class GatewayServiceError(GatewayError):
    """Gateway transport or tool execution failed."""


class GatewayProtocolError(GatewayError):
    """Unexpected endpoint, schema, envelope, or response size."""


class GatewayTimeoutError(GatewayServiceError):
    """The overall operation deadline or a transport timeout expired."""


class GatewayClient:
    def __init__(self, url: str, region: str, target_name: str, *, credentials=None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 total_timeout: float = 20, max_response_bytes: int = 1_048_576):
        try:
            parsed = urlsplit(url)
            valid = (isinstance(region, str) and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region)
                     and parsed.scheme == "https" and parsed.netloc == parsed.hostname
                     and re.fullmatch(r"[a-z0-9][a-z0-9-]*\.gateway\.bedrock-agentcore\." + re.escape(region) + r"\.amazonaws\.com", parsed.hostname or "")
                     and parsed.path == "/mcp" and not any(marker in url for marker in ("?", "#", "\\"))
                     and not any(character.isspace() for character in url))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or not isinstance(target_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", target_name):
            raise GatewayProtocolError("Invalid gateway configuration")
        if type(total_timeout) not in (int, float) or not math.isfinite(total_timeout) or total_timeout <= 0:
            raise GatewayProtocolError("Invalid gateway deadline")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise GatewayProtocolError("Invalid gateway response limit")
        self.url, self.region, self.tool_name = url, region, target_name + "___WebSearch"
        self.timeout, self.max_bytes = total_timeout, max_response_bytes
        self.credentials, self.transport = credentials, transport
        self.session = None

    def _sign(self, body: bytes, session_id: str | None) -> dict:
        try:
            if self.credentials is None:
                if self.session is None:
                    self.session = boto3.Session(region_name=self.region)
                credentials = self.session.get_credentials().get_frozen_credentials()
            else:
                credentials = self.credentials
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "Accept-Encoding": "identity", "MCP-Protocol-Version": PROTOCOL_VERSION}
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            request = AWSRequest(method="POST", url=self.url, data=body, headers=headers)
            SigV4Auth(credentials, "bedrock-agentcore", self.region).add_auth(request)
            return dict(request.headers.items())
        except Exception:
            raise GatewayAuthError("Gateway credentials unavailable") from None

    @staticmethod
    def _message(raw: bytes, request_id: str, *, sse: bool = False):
        try:
            message = json.loads(raw, object_pairs_hook=unique_json_object)
        except (ValueError, UnicodeError, RecursionError):
            raise GatewayProtocolError("Invalid gateway JSON") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise GatewayProtocolError("Invalid gateway envelope")
        if sse and "id" not in message and isinstance(message.get("method"), str) and not {"result", "error"} & message.keys():
            return None
        if message.get("id") != request_id or ("result" in message) == ("error" in message):
            raise GatewayProtocolError("Uncorrelated gateway response")
        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise GatewayProtocolError("Invalid gateway error envelope")
            raise GatewayServiceError("Gateway operation failed")
        if not isinstance(message["result"], dict):
            raise GatewayProtocolError("Invalid gateway result")
        return message

    async def _post(self, client, method, params, budget, session_id=None, *, notification=False):
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = request_id
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = await asyncio.to_thread(self._sign, body, session_id)
        async with client.stream("POST", self.url, content=body, headers=headers) as response:
            if response.status_code in (401, 403):
                raise GatewayAuthError("Gateway access denied")
            if response.status_code == 429:
                raise GatewayRateLimitError("Gateway rate limit exceeded")
            if response.status_code >= 500:
                raise GatewayServiceError("Gateway service unavailable")
            if response.status_code != (202 if notification else 200):
                raise GatewayProtocolError("Unexpected gateway HTTP status")
            session = response.headers.get("mcp-session-id")
            if session is not None and (not session or len(session) > 1024 or any(not 33 <= ord(char) <= 126 for char in session)):
                raise GatewayProtocolError("Invalid gateway session")
            if session_id and session not in (None, session_id):
                raise GatewayProtocolError("Unexpected gateway session change")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise GatewayProtocolError("Unsupported gateway content encoding")
            if not notification and content_type not in ("application/json", "text/event-stream"):
                raise GatewayProtocolError("Unsupported gateway content type")
            buffer = b""
            async for chunk in response.aiter_bytes():
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise GatewayProtocolError("Gateway response limit exceeded")
                buffer += chunk
                if notification:
                    raise GatewayProtocolError("Unexpected notification response body")
                if content_type == "text/event-stream":
                    buffer = buffer.replace(b"\r\n", b"\n")
                    while b"\n\n" in buffer:
                        event, buffer = buffer.split(b"\n\n", 1)
                        data = [line[5:].removeprefix(b" ") for line in event.split(b"\n") if line.startswith(b"data:")]
                        if data:
                            message = self._message(b"\n".join(data), request_id, sse=True)
                            if message is not None:
                                return message, session
            if notification:
                return None, session
            if content_type == "text/event-stream":
                raise GatewayProtocolError("Gateway stream ended without a result")
            return self._message(buffer, request_id), session

    def _check_tool(self, tool, arguments):
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise GatewayProtocolError("Unsupported Web Search schema")
        properties, required = schema.get("properties"), schema.get("required")
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(name, str) for name in required)
                or "query" not in required or set(required) - {"query", "maxResults"}):
            raise GatewayProtocolError("Unsupported Web Search schema")
        for name, kind in (("query", "string"), ("maxResults", "integer")):
            if not isinstance(properties.get(name), dict) or properties[name].get("type") != kind:
                raise GatewayProtocolError("Unsupported Web Search schema")
        try:
            pending = [schema]
            while pending:
                node = pending.pop()
                if isinstance(node, dict):
                    if {"$ref", "$dynamicRef"} & node.keys():
                        raise ValueError("References are unsupported")
                    pending.extend(node.values())
                elif isinstance(node, list):
                    pending.extend(node)
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
        except Exception:
            raise GatewayProtocolError("Unsupported Web Search schema or arguments") from None

    async def _search(self, arguments):
        budget = [self.max_bytes]
        async with httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                    timeout=self.timeout, trust_env=False) as client:
            initialized, session_id = await self._post(client, "initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "owui-web-search", "version": "1.0"}}, budget)
            if initialized["result"].get("protocolVersion") != PROTOCOL_VERSION:
                raise GatewayProtocolError("Unsupported negotiated MCP version")
            await self._post(client, "notifications/initialized", {}, budget, session_id, notification=True)
            cursors, params = set(), {}
            for _ in range(8):
                listed, _session = await self._post(client, "tools/list", params, budget, session_id)
                tools = listed["result"].get("tools")
                if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
                    raise GatewayProtocolError("Invalid gateway tool listing")
                matches = [tool for tool in tools if tool.get("name") == self.tool_name]
                if len(matches) > 1:
                    raise GatewayProtocolError("Ambiguous Web Search tool")
                if matches:
                    self._check_tool(matches[0], arguments)
                    break
                cursor = listed["result"].get("nextCursor")
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise GatewayProtocolError("Configured Web Search tool unavailable")
                cursors.add(cursor)
                params = {"cursor": cursor}
            else:
                raise GatewayProtocolError("Gateway discovery limit exceeded")
            envelope, _session = await self._post(client, "tools/call", {"name": self.tool_name, "arguments": arguments}, budget, session_id)
            try:
                return parse_search_response(envelope)
            except SearchToolError:
                raise GatewayServiceError("Web Search execution failed") from None
            except (SearchContractError, RecursionError):
                raise GatewayProtocolError("Invalid Web Search response") from None

    async def search(self, query: str, max_results: int = 10) -> SearchResponse:
        arguments = validate_search_input(query, max_results)
        try:
            return await asyncio.wait_for(self._search(arguments), timeout=self.timeout)
        except (TimeoutError, httpx.TimeoutException):
            raise GatewayTimeoutError("Gateway deadline exceeded") from None
        except httpx.HTTPError:
            raise GatewayServiceError("Gateway transport failed") from None
