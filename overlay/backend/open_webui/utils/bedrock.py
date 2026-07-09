# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon Bedrock provider module for Open WebUI.

Provides boto3 client initialization, model listing via ListFoundationModels,
chat completion via the Converse and ConverseStream APIs, and message format
conversion between Open Web UI internal format and Bedrock Converse API format.
"""

import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

import boto3
from botocore.exceptions import ClientError

from open_webui.config import (
    BEDROCK_REGION,
    BEDROCK_ENDPOINT_URL,
)

log = logging.getLogger(__name__)


def _get_bedrock_clients(region: str = None, endpoint_url: str = None):
    """
    Create and return Bedrock management and runtime clients.
    """
    region = region or (BEDROCK_REGION.value if hasattr(BEDROCK_REGION, "value") else BEDROCK_REGION)
    endpoint = endpoint_url or (
        BEDROCK_ENDPOINT_URL.value if hasattr(BEDROCK_ENDPOINT_URL, "value") else BEDROCK_ENDPOINT_URL
    )

    kwargs_mgmt = {"region_name": region}
    kwargs_runtime = {"region_name": region}

    if endpoint:
        kwargs_runtime["endpoint_url"] = endpoint

    bedrock_client = boto3.client("bedrock", **kwargs_mgmt)
    bedrock_runtime = boto3.client("bedrock-runtime", **kwargs_runtime)

    return bedrock_client, bedrock_runtime


def list_bedrock_models(region: str = None, endpoint_url: str = None) -> list:
    """
    List available Bedrock cross-region inference profiles for text generation.

    Prefers cross-region inference profiles (us.*, eu.*, global.*) over base
    model IDs, as most newer models require inference profiles for on-demand use.
    """
    try:
        bedrock_client, _ = _get_bedrock_clients(region, endpoint_url)

        models = []
        seen_ids = set()

        # List cross-region inference profiles (preferred for invocation)
        try:
            paginator = bedrock_client.get_paginator("list_inference_profiles")
            for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
                for profile in page.get("inferenceProfileSummaries", []):
                    profile_id = profile.get("inferenceProfileId", "")
                    profile_name = profile.get("inferenceProfileName", profile_id)
                    status = profile.get("status", "")

                    if status != "ACTIVE":
                        continue

                    # Only include text-capable profiles
                    profile_type = profile.get("type", "")
                    model_refs = profile.get("models", [])

                    models.append(
                        {
                            "id": profile_id,
                            "name": profile_name,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "bedrock",
                            "info": {
                                "inference_profile": True,
                                "streaming_supported": True,
                            },
                        }
                    )
                    seen_ids.add(profile_id)
        except ClientError as e:
            log.warning(f"Could not list inference profiles: {e}")
        except Exception as e:
            log.warning(f"Inference profile listing not available: {e}")

        # Fallback: list foundation models for any not covered by profiles
        try:
            response = bedrock_client.list_foundation_models(byOutputModality="TEXT")
            for model in response.get("modelSummaries", []):
                model_id = model.get("modelId", "")
                if model_id in seen_ids:
                    continue

                # Skip if a cross-region profile already covers this model
                # (profiles have format like us.anthropic.claude-... matching anthropic.claude-...)
                base_id = model_id
                if any(pid.endswith(base_id) or base_id in pid for pid in seen_ids):
                    continue

                model_name = model.get("modelName", model_id)
                provider_name = model.get("providerName", "Amazon")
                output_modalities = model.get("outputModalities", [])
                inference_types = model.get("inferenceTypesSupported", [])

                if "TEXT" not in output_modalities or not inference_types:
                    continue

                models.append(
                    {
                        "id": model_id,
                        "name": f"{provider_name} {model_name}",
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "bedrock",
                        "info": {
                            "inference_profile": False,
                            "provider_name": provider_name,
                            "input_modalities": model.get("inputModalities", []),
                            "output_modalities": output_modalities,
                            "inference_types": inference_types,
                            "streaming_supported": model.get("responseStreamingSupported", False),
                        },
                    }
                )
        except ClientError as e:
            log.warning(f"Could not list foundation models: {e}")

        return models

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]
        log.error(f"Bedrock model listing error [{error_code}]: {error_message}")
        return []
    except Exception as e:
        log.error(f"Error listing Bedrock models: {e}")
        return []


def _convert_messages_to_bedrock(messages: list) -> tuple:
    """
    Convert Open Web UI messages to Bedrock Converse API format.

    Returns (system_prompts, bedrock_messages) tuple.
    System messages are extracted and passed separately.
    """
    system_prompts = []
    bedrock_messages = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # System messages go into the system parameter
            if isinstance(content, str):
                system_prompts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_prompts.append({"text": block.get("text", "")})
                    elif isinstance(block, str):
                        system_prompts.append({"text": block})
            continue

        # Map assistant role
        if role == "assistant":
            bedrock_role = "assistant"
        else:
            bedrock_role = "user"

        # Convert content to Bedrock content blocks
        content_blocks = []
        if isinstance(content, str):
            if content:
                content_blocks.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    if block:
                        content_blocks.append({"text": block})
                elif isinstance(block, dict):
                    block_type = block.get("type", "text")
                    if block_type == "text":
                        text = block.get("text", "")
                        if text:
                            content_blocks.append({"text": text})
                    elif block_type == "image_url":
                        image_url = block.get("image_url", {})
                        url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                        if url.startswith("data:"):
                            # Parse base64 data URI
                            try:
                                header, data = url.split(",", 1)
                                media_type = header.split(":")[1].split(";")[0]
                                import base64

                                content_blocks.append(
                                    {
                                        "image": {
                                            "format": media_type.split("/")[1] if "/" in media_type else "png",
                                            "source": {"bytes": base64.b64decode(data)},
                                        }
                                    }
                                )
                            except Exception as e:
                                log.warning(f"Failed to parse image data URI: {e}")

        # Ensure at least one content block
        if not content_blocks:
            content_blocks.append({"text": " "})

        # Handle tool calls in assistant messages
        tool_calls = msg.get("tool_calls", [])
        if tool_calls and bedrock_role == "assistant":
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_use_block = {
                    "toolUse": {
                        "toolUseId": tc.get("id", str(uuid.uuid4())),
                        "name": func.get("name", ""),
                        "input": (
                            json.loads(func.get("arguments", "{}"))
                            if isinstance(func.get("arguments"), str)
                            else func.get("arguments", {})
                        ),
                    }
                }
                content_blocks.append(tool_use_block)

        # Handle tool role (tool result messages)
        if role == "tool":
            tool_result_block = {
                "toolResult": {
                    "toolUseId": msg.get("tool_call_id", str(uuid.uuid4())),
                    "content": [{"text": content if isinstance(content, str) else json.dumps(content)}],
                }
            }
            bedrock_messages.append({"role": "user", "content": [tool_result_block]})
            continue

        bedrock_messages.append({"role": bedrock_role, "content": content_blocks})

    return system_prompts, bedrock_messages


def _build_inference_config(
    temperature: float = None,
    top_p: float = None,
    max_tokens: int = None,
    stop: list = None,
) -> dict:
    """Build Bedrock inferenceConfig from standard parameters."""
    config = {}
    if temperature is not None:
        config["temperature"] = float(temperature)
    if top_p is not None:
        config["topP"] = float(top_p)
    if max_tokens is not None:
        config["maxTokens"] = int(max_tokens)
    if stop:
        config["stopSequences"] = stop if isinstance(stop, list) else [stop]
    return config


def _build_tool_config(tools: list = None) -> Optional[dict]:
    """Convert OpenAI-format tools to Bedrock toolConfig."""
    if not tools:
        return None

    bedrock_tools = []
    for tool in tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            bedrock_tools.append(
                {
                    "toolSpec": {
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "inputSchema": {"json": func.get("parameters", {})},
                    }
                }
            )

    if bedrock_tools:
        return {"tools": bedrock_tools}
    return None


def _convert_bedrock_response_to_openai(response: dict, model_id: str) -> dict:
    """Convert Bedrock Converse response to OpenAI-compatible format."""
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])
    usage = response.get("usage", {})

    # Extract text content
    text_content = ""
    tool_calls = []

    for block in content_blocks:
        if "text" in block:
            text_content += block["text"]
        elif "toolUse" in block:
            tool_use = block["toolUse"]
            tool_calls.append(
                {
                    "id": tool_use.get("toolUseId", str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": tool_use.get("name", ""),
                        "arguments": json.dumps(tool_use.get("input", {})),
                    },
                }
            )

    # Build OpenAI-compatible response
    result = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_content if text_content else None,
                },
                "finish_reason": _map_stop_reason(response.get("stopReason", "end_turn")),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("inputTokens", 0),
            "completion_tokens": usage.get("outputTokens", 0),
            "total_tokens": usage.get(
                "totalTokens",
                usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
            ),
        },
    }

    if tool_calls:
        result["choices"][0]["message"]["tool_calls"] = tool_calls
        result["choices"][0]["finish_reason"] = "tool_calls"

    return result


def _map_stop_reason(bedrock_reason: str) -> str:
    """Map Bedrock stop reason to OpenAI finish_reason."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "content_filtered": "content_filter",
    }
    return mapping.get(bedrock_reason, "stop")


def invoke_bedrock_converse(
    model_id: str,
    messages: list,
    temperature: float = None,
    top_p: float = None,
    max_tokens: int = None,
    stop: list = None,
    tools: list = None,
    region: str = None,
    endpoint_url: str = None,
) -> dict:
    """
    Invoke Bedrock Converse API for non-streaming chat completions.

    Returns an OpenAI-compatible response dict.
    """
    _, bedrock_runtime = _get_bedrock_clients(region, endpoint_url)

    system_prompts, bedrock_messages = _convert_messages_to_bedrock(messages)
    inference_config = _build_inference_config(temperature, top_p, max_tokens, stop)
    tool_config = _build_tool_config(tools)

    kwargs = {
        "modelId": model_id,
        "messages": bedrock_messages,
    }

    if system_prompts:
        kwargs["system"] = system_prompts
    if inference_config:
        kwargs["inferenceConfig"] = inference_config
    if tool_config:
        kwargs["toolConfig"] = tool_config

    try:
        response = bedrock_runtime.converse(**kwargs)
        return _convert_bedrock_response_to_openai(response, model_id)

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]
        log.error(f"Bedrock Converse error [{error_code}]: {error_message}")

        if error_code == "ThrottlingException":
            raise Exception("Bedrock API rate limit exceeded. Please try again shortly.")
        elif error_code == "AccessDeniedException":
            raise Exception(
                f"Access denied to Bedrock model '{model_id}'. " "Ensure the IAM role has the required permissions."
            )
        elif error_code == "ValidationException":
            raise Exception(f"Invalid request to Bedrock: {error_message}")
        elif error_code == "ModelNotReadyException":
            raise Exception(f"Bedrock model '{model_id}' is not ready. Please try again.")
        else:
            raise Exception(f"Bedrock API error: {error_message}")
    except Exception as e:
        if "Bedrock" in str(type(e).__name__) or isinstance(e, ClientError):
            raise
        log.error(f"Error invoking Bedrock Converse: {e}")
        raise


def invoke_bedrock_converse_stream(
    model_id: str,
    messages: list,
    temperature: float = None,
    top_p: float = None,
    max_tokens: int = None,
    stop: list = None,
    tools: list = None,
    region: str = None,
    endpoint_url: str = None,
) -> tuple:
    """
    Invoke Bedrock ConverseStream API for streaming chat completions.

    Returns (stream_generator, response_metadata) where stream_generator
    yields SSE-formatted chunks compatible with OpenAI's streaming format,
    and response_metadata is populated after streaming completes.
    """
    _, bedrock_runtime = _get_bedrock_clients(region, endpoint_url)

    system_prompts, bedrock_messages = _convert_messages_to_bedrock(messages)
    inference_config = _build_inference_config(temperature, top_p, max_tokens, stop)
    tool_config = _build_tool_config(tools)

    kwargs = {
        "modelId": model_id,
        "messages": bedrock_messages,
    }

    if system_prompts:
        kwargs["system"] = system_prompts
    if inference_config:
        kwargs["inferenceConfig"] = inference_config
    if tool_config:
        kwargs["toolConfig"] = tool_config

    try:
        response = bedrock_runtime.converse_stream(**kwargs)
        stream = response.get("stream", [])

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Metadata holder for usage tracking
        metadata = {"usage": {"inputTokens": 0, "outputTokens": 0}}

        def generate_sse_events():
            """Generate SSE events from Bedrock stream."""
            tool_use_id = None
            tool_name = None

            for event in stream:
                if "messageStart" in event:
                    # First event - send the role
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                elif "contentBlockStart" in event:
                    start = event["contentBlockStart"].get("start", {})
                    if "toolUse" in start:
                        tool_use_id = start["toolUse"].get("toolUseId", "")
                        tool_name = start["toolUse"].get("name", "")
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": tool_use_id,
                                                "type": "function",
                                                "function": {
                                                    "name": tool_name,
                                                    "arguments": "",
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})

                    if "text" in delta:
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": delta["text"]},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    elif "toolUse" in delta:
                        input_text = delta["toolUse"].get("input", "")
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "function": {
                                                    "arguments": input_text,
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "end_turn")
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": _map_stop_reason(stop_reason),
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                elif "metadata" in event:
                    # Capture usage metadata for tracking and emit an
                    # OpenAI-shape SSE chunk so the middleware streaming
                    # consumer can pick it up the same way it handles
                    # OpenAI's stream_options.include_usage final chunk.
                    usage = event["metadata"].get("usage", {}) or {}
                    metadata["usage"]["inputTokens"] = usage.get("inputTokens", 0)
                    metadata["usage"]["outputTokens"] = usage.get("outputTokens", 0)
                    metadata["usage"]["totalTokens"] = usage.get(
                        "totalTokens",
                        metadata["usage"]["inputTokens"] + metadata["usage"]["outputTokens"],
                    )
                    usage_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_id,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": metadata["usage"]["inputTokens"],
                            "completion_tokens": metadata["usage"]["outputTokens"],
                            "total_tokens": metadata["usage"]["totalTokens"],
                            "input_tokens": metadata["usage"]["inputTokens"],
                            "output_tokens": metadata["usage"]["outputTokens"],
                        },
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n"

            yield "data: [DONE]\n\n"

        return generate_sse_events, metadata

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]
        log.error(f"Bedrock ConverseStream error [{error_code}]: {error_message}")

        if error_code == "ThrottlingException":
            raise Exception("Bedrock API rate limit exceeded. Please try again shortly.")
        elif error_code == "AccessDeniedException":
            raise Exception(
                f"Access denied to Bedrock model '{model_id}'. " "Ensure the IAM role has the required permissions."
            )
        elif error_code == "ValidationException":
            raise Exception(f"Invalid request to Bedrock: {error_message}")
        else:
            raise Exception(f"Bedrock API error: {error_message}")
    except Exception as e:
        if isinstance(e, ClientError):
            raise
        log.error(f"Error invoking Bedrock ConverseStream: {e}")
        raise
