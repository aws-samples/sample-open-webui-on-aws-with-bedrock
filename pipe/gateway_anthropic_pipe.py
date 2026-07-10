# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
title: Claude via AgentCore Gateway
id: gateway_anthropic
description: Anthropic Claude models (Messages-API-only on Amazon Bedrock's OpenAI-compatible endpoint) served through the AgentCore inference gateway with the logged-in user's own OAuth identity.
version: 1.0.0
license: MIT-0
"""

# Amazon Bedrock's OpenAI-compatible surface (bedrock-mantle) serves Anthropic
# Claude models via the Anthropic Messages API only — they 400 on Chat
# Completions and Responses. Open WebUI's native OpenAI connections speak Chat
# Completions/Responses, so they cannot drive Claude. This manifold pipe closes
# that gap: it discovers the Claude models and translates OpenAI <-> Anthropic
# Messages, sending each request through the SAME AgentCore inference gateway as
# the native connections — authenticated with the logged-in user's own OAuth
# token, so per-user identity/governance is preserved end to end.
#
# Configuration comes from the ECS task environment (set by the CDK compute
# stack): GATEWAY_INFERENCE_URL and MANTLE_REGION. Valves override per install.

__pipe_version__ = "1.0.0"

import json
import logging
import os
import time
import uuid

import aiohttp
import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"


class Pipe:
    class Valves(BaseModel):
        GATEWAY_INFERENCE_URL: str = Field(
            default=os.environ.get("GATEWAY_INFERENCE_URL", ""),
            description="AgentCore gateway inference base URL (…/inference). Defaults to the GATEWAY_INFERENCE_URL task env var set by the CDK deployment.",
        )
        TARGET_PREFIX: str = Field(
            default="bedrock/",
            description="Gateway target qualifier prepended to model ids for routing.",
        )
        MANTLE_REGION: str = Field(
            default=os.environ.get("MANTLE_REGION", os.environ.get("AWS_REGION", "us-east-1")),
            description="Region for SigV4 model discovery against bedrock-mantle (pipe discovery runs with no user context, so it uses the task role).",
        )
        MODEL_FILTER: str = Field(
            default="",
            description="Optional comma/semicolon-separated allow-list of model ids. Empty = every anthropic.* model on Mantle.",
        )
        MAX_TOKENS_DEFAULT: int = Field(
            default=4096,
            description="max_tokens sent to the Messages API when the request does not set one.",
        )
        SIGV4_FALLBACK: bool = Field(
            default=False,
            description="OFF by default: every request uses the logged-in user's OAuth token through the gateway (per-user identity). Turn ON to fall back to the task role (SigV4, direct to Mantle) for users with no OAuth session, e.g. local-password logins — this loses per-user attribution.",
        )
        EMIT_USAGE: bool = Field(
            default=True,
            description="Emit a final OpenAI-shape usage chunk for per-response token display.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ #
    # discovery — SigV4 to Mantle (pipe discovery has no user context)
    # ------------------------------------------------------------------ #

    def _allowed(self, model_id: str) -> bool:
        raw = (self.valves.MODEL_FILTER or "").replace(",", ";")
        patterns = [p.strip() for p in raw.split(";") if p.strip()]
        return not patterns or model_id in patterns

    def pipes(self) -> list:
        try:
            region = self.valves.MANTLE_REGION.strip() or "us-east-1"
            url = f"https://bedrock-mantle.{region}.api.aws/v1/models"
            creds = boto3.Session().get_credentials().get_frozen_credentials()
            req = AWSRequest(method="GET", url=url)
            SigV4Auth(creds, "bedrock", region).add_auth(req)
            resp = requests.get(url, headers=dict(req.headers), timeout=(10, 30))
            resp.raise_for_status()
            out = []
            for m in resp.json().get("data", []):
                mid = m.get("id", "")
                # anthropic.* is exactly the Messages-only set on Mantle today.
                if mid.startswith("anthropic.") and m.get("status", "available") == "available" and self._allowed(mid):
                    name = mid.removeprefix("anthropic.").replace("-", " ").title()
                    out.append({"id": mid, "name": name})
            return out
        except Exception as e:
            log.exception(f"gateway_anthropic: discovery failed: {e}")
            return [{"id": "error", "name": f"Claude gateway discovery error: {e}"}]

    # ------------------------------------------------------------------ #
    # auth — the user's own OAuth access token, refreshed by Open WebUI
    # ------------------------------------------------------------------ #

    async def _user_bearer(self, __user__: dict, __request__) -> str | None:
        try:
            oauth_manager = __request__.app.state.oauth_manager
            user_id = __user__.get("id")
            session_id = __request__.cookies.get("oauth_session_id")
            if not session_id:
                from open_webui.models.oauth_sessions import OAuthSessions

                sessions = await OAuthSessions.get_sessions_by_user_id(user_id)
                sessions = [s for s in sessions if not (s.provider or "").startswith("mcp:")]
                if sessions:
                    session_id = sorted(sessions, key=lambda s: s.updated_at or 0)[-1].id
            if not session_id:
                return None
            token = await oauth_manager.get_oauth_token(user_id, session_id)
            return (token or {}).get("access_token")
        except Exception as e:
            log.warning(f"gateway_anthropic: no user OAuth token ({e})")
            return None

    def _sigv4_headers(self, url: str, payload: bytes) -> dict:
        region = self.valves.MANTLE_REGION.strip() or "us-east-1"
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        req = AWSRequest(method="POST", url=url, data=payload, headers={"Content-Type": "application/json"})
        SigV4Auth(creds, "bedrock", region).add_auth(req)
        return dict(req.headers)

    # ------------------------------------------------------------------ #
    # OpenAI <-> Anthropic Messages translation
    # ------------------------------------------------------------------ #

    def _to_messages_payload(self, model_id: str, body: dict) -> dict:
        system_parts = []
        messages = []

        for msg in body.get("messages", []):
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                if isinstance(content, str) and content:
                    system_parts.append(content)
                elif isinstance(content, list):
                    system_parts.extend(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                    )
                continue

            if role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", str(uuid.uuid4())),
                                "content": content if isinstance(content, str) else json.dumps(content),
                            }
                        ],
                    }
                )
                continue

            blocks = []
            if isinstance(content, str):
                if content:
                    blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text" and block.get("text"):
                            blocks.append({"type": "text", "text": block["text"]})
                        elif block.get("type") == "image_url":
                            image_url = block.get("image_url", {})
                            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                            if url.startswith("data:"):
                                try:
                                    header, data = url.split(",", 1)
                                    media_type = header.split(":")[1].split(";")[0]
                                    blocks.append(
                                        {
                                            "type": "image",
                                            "source": {"type": "base64", "media_type": media_type, "data": data},
                                        }
                                    )
                                except Exception as e:
                                    log.warning(f"gateway_anthropic: bad image data URI: {e}")

            if role == "assistant":
                for tc in msg.get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", str(uuid.uuid4())),
                            "name": fn.get("name", ""),
                            "input": json.loads(args) if isinstance(args, str) else (args or {}),
                        }
                    )

            if not blocks:
                blocks.append({"type": "text", "text": " "})
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": blocks})

        payload = {
            "model": model_id,
            "anthropic_version": ANTHROPIC_BEDROCK_VERSION,
            "max_tokens": int(body.get("max_tokens") or self.valves.MAX_TOKENS_DEFAULT),
            "messages": messages,
            "stream": bool(body.get("stream", False)),
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if body.get("temperature") is not None:
            payload["temperature"] = float(body["temperature"])
        if body.get("top_p") is not None:
            payload["top_p"] = float(body["top_p"])
        if body.get("stop"):
            stop = body["stop"]
            payload["stop_sequences"] = stop if isinstance(stop, list) else [stop]

        tools = []
        for tool in body.get("tools") or []:
            if tool.get("type") == "function":
                fn = tool.get("function", {})
                tools.append(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", "") or fn.get("name", ""),
                        "input_schema": fn.get("parameters", {}) or {"type": "object", "properties": {}},
                    }
                )
        if tools:
            payload["tools"] = tools
        return payload

    def _chunk(self, cid: str, model_id: str, delta: dict, finish: str | None = None) -> dict:
        return {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_id,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    @staticmethod
    def _finish(stop_reason: str) -> str:
        return {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
        }.get(stop_reason or "end_turn", "stop")

    def _openai_response(self, data: dict, model_id: str) -> dict:
        text, tool_calls = "", []
        for block in data.get("content", []) or []:
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", str(uuid.uuid4())),
                        "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}))},
                    }
                )
        usage = data.get("usage", {}) or {}
        message = {"role": "assistant", "content": text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else self._finish(data.get("stop_reason")),
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
        }

    # ------------------------------------------------------------------ #
    # chat entrypoint
    # ------------------------------------------------------------------ #

    async def pipe(self, body: dict, __user__: dict | None = None, __metadata__: dict | None = None, __request__=None):
        model_id = body.get("model", "")
        model_id = model_id.split(".", 1)[1] if "." in model_id else model_id  # strip "{function_id}."
        stream = bool(body.get("stream", False))
        payload = self._to_messages_payload(model_id, body)

        base = self.valves.GATEWAY_INFERENCE_URL.strip()
        bearer = await self._user_bearer(__user__ or {}, __request__) if __request__ is not None else None

        if bearer and base:
            url = f"{base.rstrip('/')}/v1/messages"
            payload["model"] = f"{self.valves.TARGET_PREFIX}{model_id}"
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"}
        elif self.valves.SIGV4_FALLBACK:
            region = self.valves.MANTLE_REGION.strip() or "us-east-1"
            url = f"https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages"
            headers = None  # signed after the payload is final
        else:
            raise Exception(
                "Claude via the gateway requires the logged-in user's OAuth session "
                "(sign in with SSO). To allow non-SSO users, enable the SIGV4_FALLBACK valve "
                "(note: that routes with the task role, losing per-user attribution)."
            )

        data = json.dumps(payload).encode()
        if headers is None:
            headers = self._sigv4_headers(url, data)

        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600, connect=15))
        try:
            resp = await session.post(url, data=data, headers=headers)
            if resp.status != 200:
                err = (await resp.text())[:400]
                raise Exception(f"Claude gateway error {resp.status}: {err}")
            if not stream:
                try:
                    return self._openai_response(await resp.json(), model_id)
                finally:
                    await session.close()
        except Exception:
            await session.close()
            raise

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        async def generate():
            input_tokens = 0
            tool_index = -1
            try:
                yield self._chunk(cid, model_id, {"role": "assistant", "content": ""})
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except (ValueError, TypeError):
                        continue
                    etype = event.get("type", "")

                    if etype == "message_start":
                        input_tokens = (event.get("message", {}).get("usage", {}) or {}).get("input_tokens", 0)
                    elif etype == "content_block_start":
                        block = event.get("content_block", {}) or {}
                        if block.get("type") == "tool_use":
                            tool_index += 1
                            yield self._chunk(
                                cid,
                                model_id,
                                {
                                    "tool_calls": [
                                        {
                                            "index": tool_index,
                                            "id": block.get("id", ""),
                                            "type": "function",
                                            "function": {"name": block.get("name", ""), "arguments": ""},
                                        }
                                    ]
                                },
                            )
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {}) or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield self._chunk(cid, model_id, {"content": delta["text"]})
                        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                            yield self._chunk(cid, model_id, {"reasoning_content": delta["thinking"]})
                        elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                            yield self._chunk(
                                cid,
                                model_id,
                                {"tool_calls": [{"index": max(tool_index, 0), "function": {"arguments": delta["partial_json"]}}]},
                            )
                    elif etype == "message_delta":
                        stop_reason = (event.get("delta", {}) or {}).get("stop_reason")
                        usage = event.get("usage", {}) or {}
                        if stop_reason:
                            yield self._chunk(cid, model_id, {}, self._finish(stop_reason))
                        if self.valves.EMIT_USAGE and usage.get("output_tokens") is not None:
                            yield {
                                "id": cid,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_id,
                                "choices": [],
                                "usage": {
                                    "prompt_tokens": input_tokens,
                                    "completion_tokens": usage.get("output_tokens", 0),
                                    "total_tokens": input_tokens + usage.get("output_tokens", 0),
                                    "input_tokens": input_tokens,
                                    "output_tokens": usage.get("output_tokens", 0),
                                },
                            }
            finally:
                await session.close()

        return generate()
