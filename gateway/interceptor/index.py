# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore Gateway REQUEST interceptor: capability-filtered model listing.

The gateway's /inference/v1/models endpoint otherwise returns the provider's
full catalog, including models that only support a different API (e.g. Anthropic
Claude is Messages-only, so it 400s on chat/completions). Open WebUI would then
surface models in the dropdown that fail when a user picks them.

This interceptor short-circuits the models listing with a synthetic, capability-
verified list. The list is chosen per Open WebUI connection via the
`x-models-flavor` request header (chat_completions | responses | messages);
the default is chat_completions. All other paths (chat/completions, responses,
messages, streaming) pass through untouched.

Capability lists come from the MODEL_CAPS environment variable (JSON), which the
GatewayStack populates from config/model-capabilities.json.
"""

import base64
import json
import os

CAPS = json.loads(os.environ.get("MODEL_CAPS", "{}"))
TARGET_PREFIX = os.environ.get("TARGET_PREFIX", "bedrock/")
DEFAULT_FLAVOR = "chat_completions"


def _passthrough(http):
    out = {}
    req = http.get("gatewayRequest") or {}
    if req.get("body") is not None:
        out["transformedGatewayRequest"] = {"body": req["body"]}
    return {"interceptorOutputVersion": "1.0", "http": out}


def lambda_handler(event, context):
    http = event.get("http") or {}

    # Only REQUEST interception is configured; be defensive if a response arrives.
    if http.get("gatewayResponse"):
        return {"interceptorOutputVersion": "1.0", "http": {}}

    req = http.get("gatewayRequest") or {}
    path = req.get("path", "")

    # NOTE: the gateway reports httpMethod as POST to the interceptor even for
    # the GET /v1/models listing, so match on path alone.
    if not path.rstrip("/").endswith("/models"):
        return _passthrough(http)

    headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
    flavor = headers.get("x-models-flavor", DEFAULT_FLAVOR)
    allow = CAPS.get(flavor, CAPS.get(DEFAULT_FLAVOR, []))

    body = {
        "object": "list",
        "data": [
            {"id": f"{TARGET_PREFIX}{m}", "object": "model", "owned_by": "system"}
            for m in sorted(allow)
        ],
    }
    return {
        "interceptorOutputVersion": "1.0",
        "http": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "contentType": "application/json",
                "body": base64.b64encode(json.dumps(body).encode()).decode(),
            }
        },
    }
