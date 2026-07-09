# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Amazon Bedrock router for Open WebUI.

Provides REST API endpoints for listing Bedrock models, chat completions
(streaming and non-streaming), and admin configuration management.

Usage limits and consumption tracking are out of scope for this sample; the
provider emits OpenAI-shape usage fields so Open WebUI's native per-response
usage display works unchanged.
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.bedrock import (
    list_bedrock_models,
    invoke_bedrock_converse,
    invoke_bedrock_converse_stream,
)
from open_webui.models.config import Config
from open_webui.models.models import Models
from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_system_prompt_to_body,
)

log = logging.getLogger(__name__)

router = APIRouter()


############################
# Config
############################

# v0.10 config port: upstream removed the PersistentConfig class and the
# app.state.config.* mechanism. Bedrock settings now live as per-key rows in
# the Config table under the `bedrock.*` namespace, seeded from env via
# DEFAULT_CONFIG in config.py. This map mirrors upstream's OPENAI_CONFIG_KEYS
# in routers/openai.py: {form_field: dotted_storage_key}.
BEDROCK_CONFIG_KEYS = {
    'ENABLE_BEDROCK_API': 'bedrock.enable',
    'BEDROCK_REGION': 'bedrock.region',
    'BEDROCK_ENDPOINT_URL': 'bedrock.endpoint_url',
    'BEDROCK_ALLOWED_MODELS': 'bedrock.allowed_models',
}


async def get_bedrock_runtime_config() -> tuple[bool, str, str, list]:
    """Read the Bedrock runtime settings from the Config table in one round-trip."""
    values = await Config.get_many(*BEDROCK_CONFIG_KEYS.values())
    return (
        values.get('bedrock.enable'),
        values.get('bedrock.region'),
        values.get('bedrock.endpoint_url'),
        values.get('bedrock.allowed_models') or [],
    )


############################
# Config Endpoints
############################


class BedrockConfigForm(BaseModel):
    ENABLE_BEDROCK_API: Optional[bool] = None
    BEDROCK_REGION: Optional[str] = None
    BEDROCK_ENDPOINT_URL: Optional[str] = None
    BEDROCK_ALLOWED_MODELS: Optional[list] = None


@router.get('/config')
async def get_bedrock_config(request: Request, user=Depends(get_admin_user)):
    """Return current Bedrock configuration (admin only)."""
    values = await Config.get_many(*BEDROCK_CONFIG_KEYS.values())
    return {field: values.get(storage_key) for field, storage_key in BEDROCK_CONFIG_KEYS.items()}


@router.post('/config/update')
async def update_bedrock_config(request: Request, form_data: dict, user=Depends(get_admin_user)):
    """Update Bedrock configuration (admin only)."""
    updates = {BEDROCK_CONFIG_KEYS[field]: form_data[field] for field in BEDROCK_CONFIG_KEYS if field in form_data}
    if updates:
        await Config.upsert(updates)

    return await get_bedrock_config(request, user)


############################
# Model Listing
############################


async def get_all_models(request: Request) -> list:
    """
    Get all available Bedrock models, filtered by allowed models config.
    """
    enabled, region, endpoint_url, allowed = await get_bedrock_runtime_config()
    if not enabled:
        return []

    models = list_bedrock_models(region=region, endpoint_url=endpoint_url)

    # Filter by allowed models patterns
    if allowed:
        import fnmatch

        filtered = []
        for model in models:
            for pattern in allowed:
                if fnmatch.fnmatch(model['id'], pattern):
                    filtered.append(model)
                    break
        models = filtered

    return models


@router.get('/models')
async def get_models(request: Request, user=Depends(get_verified_user)):
    """List available Bedrock models for the current user."""
    models = await get_all_models(request)

    return {'data': models}


############################
# Chat Completions
############################


class ChatCompletionMessage(BaseModel):
    role: str
    content: object = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None


class ChatCompletionForm(BaseModel):
    model: str
    messages: list
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[list] = None
    tools: Optional[list] = None


async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=None,
):
    """
    Generate a Bedrock chat completion from the middleware routing layer.
    Wraps the chat_completions endpoint logic for internal use.

    Usage is returned in OpenAI-compatible fields; enforcement of usage
    limits is not performed in this sample.
    """
    enabled, region, endpoint_url, _allowed = await get_bedrock_runtime_config()
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Bedrock API is not enabled',
        )

    model_id = form_data.get('model', '')
    messages = form_data.get('messages', [])

    # Resolve custom model ID to base model ID and apply model params/system prompt
    # (same pattern as ollama/openai routers)
    model_info = await Models.get_model_by_id(model_id)
    if model_info:
        if model_info.base_model_id:
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        if params:
            system = params.pop('system', None)
            form_data = apply_model_params_to_body_openai(params, form_data)
            form_data = apply_system_prompt_to_body(system, form_data, form_data.get('metadata'), user)
            messages = form_data.get('messages', [])

    stream = form_data.get('stream', False)

    if stream:
        generate_sse, metadata = invoke_bedrock_converse_stream(
            model_id=model_id,
            messages=messages,
            temperature=form_data.get('temperature'),
            top_p=form_data.get('top_p'),
            max_tokens=form_data.get('max_tokens'),
            stop=form_data.get('stop'),
            tools=form_data.get('tools'),
            region=region,
            endpoint_url=endpoint_url,
        )

        async def stream_iter():
            for chunk in generate_sse():
                yield chunk

        return StreamingResponse(
            stream_iter(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            },
        )
    else:
        return invoke_bedrock_converse(
            model_id=model_id,
            messages=messages,
            temperature=form_data.get('temperature'),
            top_p=form_data.get('top_p'),
            max_tokens=form_data.get('max_tokens'),
            stop=form_data.get('stop'),
            tools=form_data.get('tools'),
            region=region,
            endpoint_url=endpoint_url,
        )


@router.post('/chat/completions')
async def chat_completions(
    request: Request,
    form_data: ChatCompletionForm,
    user=Depends(get_verified_user),
):
    """
    Process chat completion requests through Bedrock Converse API.
    Supports both streaming and non-streaming responses.

    Usage is returned in OpenAI-compatible fields; enforcement of usage
    limits is not performed in this sample.
    """
    enabled, region, endpoint_url, _allowed = await get_bedrock_runtime_config()
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Bedrock API is not enabled',
        )

    model_id = form_data.model
    messages = [msg if isinstance(msg, dict) else msg.dict() for msg in form_data.messages]

    try:
        if form_data.stream:
            # Streaming response
            generate_sse, metadata = invoke_bedrock_converse_stream(
                model_id=model_id,
                messages=messages,
                temperature=form_data.temperature,
                top_p=form_data.top_p,
                max_tokens=form_data.max_tokens,
                stop=form_data.stop,
                tools=form_data.tools,
                region=region,
                endpoint_url=endpoint_url,
            )

            async def stream_iter():
                for chunk in generate_sse():
                    yield chunk

            return StreamingResponse(
                stream_iter(),
                media_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no',
                },
            )
        else:
            # Non-streaming response
            return invoke_bedrock_converse(
                model_id=model_id,
                messages=messages,
                temperature=form_data.temperature,
                top_p=form_data.top_p,
                max_tokens=form_data.max_tokens,
                stop=form_data.stop,
                tools=form_data.tools,
                region=region,
                endpoint_url=endpoint_url,
            )

    except Exception as e:
        log.error(f'Bedrock chat completion error: {e}')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
