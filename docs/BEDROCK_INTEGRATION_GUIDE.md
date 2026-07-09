# Amazon Bedrock Integration Guide for Open WebUI

This document is the technical reference for the native Amazon Bedrock integration shipped in [`aws-samples/sample-open-webui-on-aws-with-bedrock`](https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock). It covers the architecture, API translation layer, model discovery, message format conversion, streaming implementation, model access control, usage reporting, and IAM configuration. This guide is intended for developers extending the integration, solutions architects evaluating the approach, or teams building similar Bedrock integrations in other applications.

---

## Table of Contents

1. [Integration Overview](#1-integration-overview)
2. [Architecture](#2-architecture)
3. [Module Reference](#3-module-reference)
4. [Model Discovery](#4-model-discovery)
5. [Message Format Translation](#5-message-format-translation)
6. [Chat Completions](#6-chat-completions)
7. [Streaming Implementation](#7-streaming-implementation)
8. [Tool Calling (Function Calling)](#8-tool-calling-function-calling)
9. [Cognito Group-Based Access Control](#9-cognito-group-based-access-control)
10. [Model Access Control](#10-model-access-control)
11. [Usage Reporting](#11-usage-reporting)
12. [IAM Permissions](#12-iam-permissions)
13. [Configuration Reference](#13-configuration-reference)
14. [Key Design Decisions](#14-key-design-decisions)
15. [CI/CD Pipeline Integration](#15-cicd-pipeline-integration)
16. [File Inventory](#16-file-inventory)
17. [Applying This Pattern to Other Projects](#17-applying-this-pattern-to-other-projects)

---

## 1. Integration Overview

Open WebUI's upstream architecture supports two LLM providers: Ollama (local models) and OpenAI-compatible APIs (proxied HTTP). Both use the OpenAI chat completion format as the internal lingua franca.

This sample adds a **third, native provider** that communicates directly with Amazon Bedrock's Converse API via boto3 — no OpenAI-compatible proxy, no LiteLLM, no API keys. The integration:

- Uses **IAM role credentials** (ECS task role) instead of API keys
- Calls the **Converse and ConverseStream APIs** directly (not InvokeModel)
- Translates between **OpenAI chat completion format** (used internally by Open WebUI) and **Bedrock Converse format** at the boundary
- Discovers models via **ListInferenceProfiles** (cross-region) with **ListFoundationModels** fallback
- Gates model visibility with **Open WebUI's native per-model access grants** (driven by Cognito group sync)
- Emits **OpenAI-shape usage fields** on every response, so Open WebUI's native per-response usage display works unchanged

Per-user token accounting and quota enforcement are intentionally **not** part of this sample — see [Model Access Control](#10-model-access-control) for what is included and the AWS-native alternatives.

### How the Code Ships: Overlay + Patches on the Official Image

This repository never vendors Open WebUI source. Instead, the integration is applied onto the **official Open WebUI release image, pinned to v0.10.2**, at Docker build time:

- **`overlay/`** — new files copied into the image as-is: the two backend modules (`overlay/backend/open_webui/routers/bedrock.py`, `overlay/backend/open_webui/utils/bedrock.py`) and one frontend API client (`overlay/src/lib/apis/bedrock/index.ts`).
- **`patches/backend/`** — 5 small, attributed unified diffs that wire the overlay modules into upstream files (`config.py`, `env.py`, `main.py`, `utils/chat.py`, `utils/models.py`). Applied with `git apply` in an ephemeral Docker build stage.
- **`patches/frontend/`** — 2 diffs that add an "Amazon Bedrock API" section to the admin Connections settings panel. Used only by the opt-in `full` image target, which rebuilds the UI from upstream source at the pinned tag.

The `Dockerfile` exposes two targets:

| Target | Default? | Contents |
|---|---|---|
| `backend` | Yes (last stage) | Official image + 2 overlay backend modules + 5 backend patches. The official UI ships unchanged; Bedrock is configured via environment variables or the REST admin API. |
| `full` | Opt-in (`--target full`) | Everything in `backend`, plus a UI rebuilt from upstream source with the admin Connections Bedrock panel. |

`docker/apply-patches.sh` verifies in CI that every patch still applies cleanly against a pristine upstream checkout at the pinned tag. When file paths like `backend/open_webui/routers/bedrock.py` appear in this guide, they refer to the path **inside the built image**; in this repository the source lives under `overlay/` and `patches/`.

### Why Converse API (Not InvokeModel)

The Converse API provides a **model-agnostic interface** across all Bedrock models. Unlike InvokeModel, which requires model-specific request/response formats (Anthropic's format differs from Amazon's, which differs from Meta's), Converse normalizes:

- Message structure (roles, content blocks)
- System prompts (separate parameter)
- Tool/function calling (unified toolConfig)
- Streaming events (consistent event types)
- Usage metrics (inputTokens, outputTokens in metadata)

This means the integration works with **any current or future Bedrock model** without model-specific code paths.

---

## 2. Architecture

### Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Open WebUI Backend                           │
│                                                                     │
│  POST /api/chat/completions                                         │
│       │                                                             │
│       ▼                                                             │
│  main.py ──► middleware.py (tools, RAG, filters)                    │
│       │                                                             │
│       ▼                                                             │
│  chat.py ──► Route by model.owned_by                                │
│       │         │              │              │                     │
│       │     "ollama"       "bedrock"       default                  │
│       │         │              │           (openai)                 │
│       │         ▼              ▼              │                     │
│       │    Ollama API    ┌─────────────┐     ▼                     │
│       │                  │ bedrock.py  │  OpenAI API               │
│       │                  │  (router)   │                           │
│       │                  └──────┬──────┘                           │
│       │                         │                                   │
│       │                         ▼                                   │
│       │                  ┌─────────────┐                           │
│       │                  │ bedrock.py  │                           │
│       │                  │  (utils)    │                           │
│       │                  └──────┬──────┘                           │
│       │                         │                                   │
│       │              ┌──────────┼──────────┐                       │
│       │              │          │          │                       │
│       │         Convert    Build IAM   Convert                     │
│       │         Messages   boto3 call  Response                    │
│       │         to Bedrock             to OpenAI                   │
│       │              │          │          │                       │
└───────┼──────────────┼──────────┼──────────┼───────────────────────┘
        │              │          │          │
        │              ▼          ▼          ▼
        │         ┌──────────────────────────────┐
        │         │     Amazon Bedrock           │
        │         │  Converse / ConverseStream   │
        │         │  (cross-region inference)    │
        │         └──────────────────────────────┘
        │
        ▼
   Response to client (OpenAI format)
```

### Module Dependency Graph

```
routers/bedrock.py          ◄── overlay: FastAPI endpoints + generate_chat_completion wrapper
    └── utils/bedrock.py    ◄── overlay: boto3 Converse API, message conversion, streaming

utils/chat.py               ◄── patch 0004: provider routing (owned_by == "bedrock")
    └── routers/bedrock.py::generate_chat_completion

utils/models.py             ◄── patch 0005: model aggregation (parallel fetch)
    └── routers/bedrock.py::get_all_models

infra/lib/bedrock-access-construct.ts ◄── IAM policy for ECS task role
```

Note: Authentication is handled by Open WebUI's built-in OIDC support (configured via env vars for Cognito). Model access control uses Open WebUI's native RBAC (groups synced from Cognito → model access grants on the Models table). No custom auth or access control code.

---

## 3. Module Reference

| Module (path in this repo) | Purpose | Key Functions |
|---|---|---|
| `overlay/backend/open_webui/utils/bedrock.py` | Core Bedrock SDK wrapper | `list_bedrock_models()`, `invoke_bedrock_converse()`, `invoke_bedrock_converse_stream()`, `_convert_messages_to_bedrock()`, `_convert_bedrock_response_to_openai()` |
| `overlay/backend/open_webui/routers/bedrock.py` | REST API endpoints | `get_all_models()`, `get_models()`, `chat_completions()`, `generate_chat_completion()` |
| `overlay/src/lib/apis/bedrock/index.ts` | Frontend API client (used by the `full` target's admin panel) | `getBedrockConfig()`, `updateBedrockConfig()` |
| `infra/lib/bedrock-access-construct.ts` | IAM permissions | CDK construct generating Bedrock IAM policy statements |
| `scripts/set-model-access.sh` | Model access management | Bulk grant model access by group via Open WebUI's REST API |

The five patches under `patches/backend/` wire these modules into the upstream application (config defaults, env vars, router registration, chat dispatch, model aggregation) — see the [File Inventory](#16-file-inventory).

---

## 4. Model Discovery

### Strategy: Inference Profiles First, Foundation Models Fallback

```python
# utils/bedrock.py::list_bedrock_models()

# Step 1: List cross-region inference profiles (preferred)
paginator = bedrock_client.get_paginator("list_inference_profiles")
for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
    for profile in page["inferenceProfileSummaries"]:
        # Only ACTIVE profiles
        # Returns IDs like: us.anthropic.claude-sonnet-4-5-v2:0

# Step 2: Fallback to foundation models (for models without profiles)
response = bedrock_client.list_foundation_models(byOutputModality="TEXT")
for model in response["modelSummaries"]:
    # Skip if already covered by an inference profile
    # Returns IDs like: anthropic.claude-3-haiku-20240307-v1:0
```

### Why Inference Profiles

Most newer Bedrock models (Claude Sonnet 4, Nova, etc.) require **cross-region inference profile IDs** for on-demand invocation. Using the base foundation model ID returns an error. Inference profiles:

- Provide **cross-region routing** (e.g., `us.anthropic.claude-*` routes across US regions)
- Are the **recommended invocation method** for on-demand models
- Include **all models accessible** in the account (respects model access grants)

### Model Object Format

Every model returned by the discovery layer uses this structure, compatible with OpenAI's model list format:

```json
{
  "id": "us.anthropic.claude-sonnet-4-5-v2:0",
  "name": "Claude Sonnet 4.5 v2",
  "object": "model",
  "created": 1711234567,
  "owned_by": "bedrock",
  "info": {
    "inference_profile": true,
    "streaming_supported": true
  }
}
```

The `owned_by: "bedrock"` field is critical — it's how the chat routing layer (`utils/chat.py`) identifies which provider to use. Bedrock models appear alongside OpenAI/Ollama models in the standard `/api/models` listing (fetched in parallel via `utils/models.py`), so the rest of Open WebUI — model selector, per-model access grants, admin model settings — treats them like any other model.

---

## 5. Message Format Translation

The core translation challenge: Open WebUI uses **OpenAI chat completion format** internally, but Bedrock's Converse API uses a **different message structure**. The `_convert_messages_to_bedrock()` function handles this bidirectionally.

### OpenAI → Bedrock Conversion

| OpenAI Format | Bedrock Converse Format | Notes |
|---|---|---|
| `{"role": "system", "content": "..."}` | Extracted to `system` parameter | Bedrock separates system prompts from messages |
| `{"role": "user", "content": "text"}` | `{"role": "user", "content": [{"text": "..."}]}` | Content becomes an array of content blocks |
| `{"role": "assistant", "content": "text"}` | `{"role": "assistant", "content": [{"text": "..."}]}` | Same block structure |
| `{"role": "user", "content": [{"type": "text", ...}, {"type": "image_url", ...}]}` | `{"role": "user", "content": [{"text": "..."}, {"image": {"format": "png", "source": {"bytes": ...}}}]}` | Images: data URI → binary bytes |
| `{"role": "assistant", "tool_calls": [...]}` | `{"role": "assistant", "content": [{"toolUse": {"toolUseId": "...", "name": "...", "input": {...}}}]}` | Tool calls become toolUse content blocks |
| `{"role": "tool", "tool_call_id": "...", "content": "..."}` | `{"role": "user", "content": [{"toolResult": {"toolUseId": "...", "content": [{"text": "..."}]}}]}` | Tool results are user messages with toolResult blocks |

### Bedrock → OpenAI Conversion

The `_convert_bedrock_response_to_openai()` function converts the Converse API response back:

```python
# Bedrock Converse response:
{
    "output": {
        "message": {
            "role": "assistant",
            "content": [{"text": "Hello!"}, {"toolUse": {...}}]
        }
    },
    "stopReason": "end_turn",
    "usage": {"inputTokens": 10, "outputTokens": 5}
}

# Converted to OpenAI format:
{
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "model": "us.anthropic.claude-sonnet-4-5-v2:0",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Hello!", "tool_calls": [...]},
        "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
}
```

### Stop Reason Mapping

| Bedrock `stopReason` | OpenAI `finish_reason` |
|---|---|
| `end_turn` | `stop` |
| `max_tokens` | `length` |
| `stop_sequence` | `stop` |
| `tool_use` | `tool_calls` |
| `content_filtered` | `content_filter` |

### Inference Config Translation

| OpenAI Parameter | Bedrock `inferenceConfig` Field |
|---|---|
| `temperature` | `temperature` |
| `top_p` | `topP` |
| `max_tokens` | `maxTokens` |
| `stop` | `stopSequences` |

---

## 6. Chat Completions

### Request Flow

1. **Frontend** sends `POST /api/chat/completions` with OpenAI-format body
2. **main.py** resolves the model from the model cache (populated by model aggregation) — Open WebUI's native access check rejects models the user's groups don't have access to
3. **middleware.py** processes the payload (tools, RAG, filters, system prompts)
4. **chat.py** routes based on `model.owned_by`:
   ```python
   if model.get("owned_by") == "ollama":
       # Ollama path
   elif model.get("owned_by") == "bedrock":
       return await generate_bedrock_chat_completion(request, form_data, user)
   else:
       # OpenAI path (default)
   ```
5. **routers/bedrock.py::generate_chat_completion()** performs:
   - Provider-enabled check (reads the `bedrock.*` keys from the Config table; 400 if disabled)
   - Custom-model resolution (workspace models with a `base_model_id` resolve to the underlying Bedrock model)
   - Model params + system prompt application (`apply_model_params_to_body_openai`, `apply_system_prompt_to_body` — same pattern as the ollama/openai routers)
   - Delegates to `invoke_bedrock_converse()` or `invoke_bedrock_converse_stream()`
6. **utils/bedrock.py** converts messages, calls boto3, converts response
7. Response flows back through middleware (outlet filters, usage fields attached to the message)

### Non-Streaming (Converse API)

```python
# utils/bedrock.py::invoke_bedrock_converse()

system_prompts, bedrock_messages = _convert_messages_to_bedrock(messages)
inference_config = _build_inference_config(temperature, top_p, max_tokens, stop)
tool_config = _build_tool_config(tools)

response = bedrock_runtime.converse(
    modelId=model_id,          # e.g., "us.anthropic.claude-sonnet-4-5-v2:0"
    messages=bedrock_messages,
    system=system_prompts,      # Separate from messages
    inferenceConfig=inference_config,
    toolConfig=tool_config,
)

return _convert_bedrock_response_to_openai(response, model_id)
```

### Error Handling

`utils/bedrock.py` maps boto3 `ClientError` codes to actionable messages instead of surfacing raw SDK errors:

| Bedrock error code | Surfaced as |
|---|---|
| `ThrottlingException` | "Bedrock API rate limit exceeded. Please try again shortly." |
| `AccessDeniedException` | "Access denied to Bedrock model '<id>'. Ensure the IAM role has the required permissions." |
| `ValidationException` | "Invalid request to Bedrock: <message>" |
| *(other)* | "Bedrock API error: <message>" |

An `AccessDeniedException` at runtime almost always means either the model isn't enabled in the Bedrock console (model access) or the task role's IAM policy doesn't cover the model/profile ARN — see [IAM Permissions](#12-iam-permissions).

---

## 7. Streaming Implementation

### Bedrock ConverseStream → OpenAI SSE

The Bedrock ConverseStream API returns an event stream with typed events. The integration converts each event to an OpenAI-compatible SSE chunk in real-time:

| Bedrock Stream Event | OpenAI SSE Chunk | Description |
|---|---|---|
| `messageStart` | `{"delta": {"role": "assistant", "content": ""}}` | Stream begins, send role |
| `contentBlockStart` (text) | *(no output)* | Text block starting |
| `contentBlockStart` (toolUse) | `{"delta": {"tool_calls": [{"id": "...", "function": {"name": "..."}}]}}` | Tool call begins |
| `contentBlockDelta` (text) | `{"delta": {"content": "token"}}` | Text token |
| `contentBlockDelta` (toolUse) | `{"delta": {"tool_calls": [{"function": {"arguments": "..."}}]}}` | Tool call argument chunk |
| `messageStop` | `{"delta": {}, "finish_reason": "stop"}` | Stream ends |
| `metadata` | `{"choices": [], "usage": {...}}` | Final usage chunk (see below) |
| *(end)* | `data: [DONE]\n\n` | SSE termination signal |

### SSE Format

Each chunk is yielded as a Server-Sent Events line:

```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1711234567,"model":"us.anthropic.claude-sonnet-4-5-v2:0","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

```

The router returns this generator wrapped in a `StreamingResponse` with `media_type="text/event-stream"` and `X-Accel-Buffering: no` so proxies don't buffer the stream.

### Usage Metadata Capture

The Bedrock stream emits a `metadata` event after the final content event, containing token counts. The integration does two things with it:

1. **Emits a final SSE chunk** with an empty `choices` array and a `usage` block — the same shape OpenAI produces for `stream_options.include_usage=true`, which Open WebUI's streaming consumer already understands (see [Usage Reporting](#11-usage-reporting)).
2. **Populates a shared `metadata` dict** returned alongside the generator, so callers can read the totals after the stream completes:

```python
generate_sse, metadata = invoke_bedrock_converse_stream(...)
# metadata = {"usage": {"inputTokens": 0, "outputTokens": 0}}
# After streaming completes, metadata["usage"] holds the actual counts
```

---

## 8. Tool Calling (Function Calling)

### OpenAI Tools → Bedrock toolConfig

```python
# OpenAI format (input):
{"type": "function", "function": {
    "name": "get_weather",
    "description": "Get weather for a location",
    "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
}}

# Bedrock format (converted):
{"toolSpec": {
    "name": "get_weather",
    "description": "Get weather for a location",
    "inputSchema": {"json": {"type": "object", "properties": {"location": {"type": "string"}}}}
}}
```

### Tool Call Response Flow

1. Bedrock returns `stopReason: "tool_use"` with `toolUse` content blocks
2. Integration converts to OpenAI `tool_calls` format
3. Open WebUI's middleware executes the tool
4. Tool result is sent back as a `{"role": "tool", "tool_call_id": "...", "content": "..."}` message
5. Integration converts tool result to Bedrock `toolResult` content block
6. Next Converse call includes the tool result for the model to process

---

## 9. Cognito Group-Based Access Control

### How It Works

Model access uses Open WebUI's **native RBAC system**, not custom code:

```
User authenticates via Cognito (built-in OIDC)
    → cognito:groups claim in ID token
    → ENABLE_OAUTH_GROUP_MANAGEMENT syncs groups to Open WebUI
    → Admin grants model access to groups via UI or API
    → Users see only models their groups have access to
```

### Setup

1. **Cognito groups** (e.g., `basic-users`, `power-users`, `faculty`) are created in the Cognito User Pool
2. **OAuth Group Management** (`ENABLE_OAUTH_GROUP_MANAGEMENT=true`) syncs these to Open WebUI groups on each login
3. **Admin configures model access** in Workspace → Models — see [Model Access Control](#10-model-access-control)
4. **Bulk operations** via `scripts/set-model-access.sh`

### Key Configuration

```bash
# Cognito group sync (set by the CDK compute stack / deploy.sh)
ENABLE_OAUTH_GROUP_MANAGEMENT=true
OAUTH_GROUP_CLAIM=cognito:groups
ENABLE_OAUTH_GROUP_CREATION=true
OAUTH_USERNAME_CLAIM=email  # Required for Cognito — ensures ID token is used (has groups)

# Role mapping
ENABLE_OAUTH_ROLE_MANAGEMENT=true
OAUTH_ROLES_CLAIM=cognito:groups
OAUTH_ADMIN_ROLES=admin,webui-admins,admins

# New users start as pending until added to a Cognito group
DEFAULT_USER_ROLE=pending
```

---

## 10. Model Access Control

Model access in this sample is layered, coarsest to finest. All three layers are standard AWS or native Open WebUI mechanisms — there is no custom access-control code in the Bedrock provider.

### Layer 1: IAM policy (account/deployment level)

The ECS task role's IAM policy determines which models the *deployment* can invoke at all. Pass `allowedModels` to the construct in `infra/lib/bedrock-access-construct.ts` to restrict this — see [IAM Permissions](#12-iam-permissions).

### Layer 2: `BEDROCK_ALLOWED_MODELS` (admin allow-list)

A comma-separated list of model ID glob patterns (e.g., `us.anthropic.*,us.amazon.nova*`). Only matching models are exposed by the provider at all — to any user, in any list. Empty means "expose everything discovery returns". Set it via env var or the admin config API; it's the right tool for keeping the model list short and intentional.

### Layer 3: Open WebUI's native per-model access grants (per-group)

Fine-grained, per-group visibility uses Open WebUI's built-in access grants on the Models table — the same mechanism used for OpenAI/Ollama models:

- **Admin UI:** Workspace → Models → edit a model → set visibility to **Private** → select the groups that should see it. Public models are visible to everyone; private models only to granted groups.
- **Bulk, via script:** `scripts/set-model-access.sh` drives the same grants through the REST API:

```
./set-model-access.sh --url URL --token TOKEN --group GROUP --pattern PATTERN [--permission read|write]
./set-model-access.sh --url URL --token TOKEN --list-groups
./set-model-access.sh --url URL --token TOKEN --list-models
./set-model-access.sh --url URL --token TOKEN --show-access MODEL_ID
```

```bash
# Grant basic-users read access to all Nova models
./scripts/set-model-access.sh --url https://oui.example.com --token $TOKEN \
    --group basic-users --pattern "us.amazon.nova*"

# Grant power-users read access to ALL Bedrock models
./scripts/set-model-access.sh --url https://oui.example.com --token $TOKEN \
    --group power-users --pattern "*"

# Inspect current grants for one model
./scripts/set-model-access.sh --url https://oui.example.com --token $TOKEN \
    --show-access "us.anthropic.claude-sonnet-4-5-v2:0"
```

Notes on the script's behavior:

- `--token` is an admin user's JWT — copy the `token` cookie from your browser after SSO login, or use an API key.
- `--pattern` uses shell glob matching (Python `fnmatch`): `*` matches anything.
- Grants are **additive** — running the script for a second group adds that group; it does not replace existing grants (duplicates are deduplicated).
- `--permission` defaults to `read` (visibility/use); `write` additionally allows editing the model entry.

### What This Sample Does *Not* Include

**Token-level accounting and quota enforcement are not included in this sample.** The provider reports token usage on every response (see [Usage Reporting](#11-usage-reporting)), but nothing in this codebase counts tokens per user over time, enforces spending limits, or rejects requests when a budget is exhausted. If you need usage governance, use the AWS-native building blocks:

- **Bedrock service quotas** — account-level requests-per-minute and tokens-per-minute limits per model, managed in Service Quotas.
- **CloudWatch metrics for Bedrock** — `Invocations`, `InputTokenCount`, `OutputTokenCount`, latency and throttling metrics per model; alarm on them or build dashboards.
- **Application inference profiles** — create per-team/per-app inference profiles and tag them for cost allocation, so Bedrock spend shows up split by workload in Cost Explorer.

---

## 11. Usage Reporting

The provider emits **OpenAI-shape usage fields** on every response, so Open WebUI's native per-response usage display (the info shown on each chat message) works unchanged. There are no custom usage tables, no aggregation, and no admin usage dashboards in this sample.

- **Non-streaming (Converse).** `_convert_bedrock_response_to_openai()` copies `usage.inputTokens` / `usage.outputTokens` from the Converse response into an OpenAI-shape `usage = {prompt_tokens, completion_tokens, total_tokens}` block on the completion object.
- **Streaming (ConverseStream).** On the stream's `metadata` event, `invoke_bedrock_converse_stream()` emits a final `chat.completion.chunk` with an **empty `choices` array** and a `usage` block carrying both OpenAI-compatible fields (`prompt_tokens`, `completion_tokens`, `total_tokens`) and Bedrock-native fields (`input_tokens`, `output_tokens`). This matches the OpenAI `stream_options.include_usage=true` contract that Open WebUI's streaming consumer already handles, so token counts appear on streamed responses too.

For fleet-level usage analytics, rely on CloudWatch's Bedrock metrics and application inference profiles rather than the application layer (see the previous section).

---

## 12. IAM Permissions

The CDK construct (`infra/lib/bedrock-access-construct.ts`) generates two IAM policy statements for the ECS task role:

### Statement 1: Model and Profile Discovery

```json
{
    "Effect": "Allow",
    "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles",
        "bedrock:GetInferenceProfile"
    ],
    "Resource": "*"
}
```

### Statement 2: Model Invocation

```json
{
    "Effect": "Allow",
    "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream"
    ],
    "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:*:inference-profile/*"
    ]
}
```

The wildcard region (`*`) in the resource ARN is required for cross-region inference profiles, which route requests across multiple regions.

### Restricting to Specific Models

Pass `allowedModels` when instantiating the construct (see `infra/lib/compute-stack.ts`) to restrict IAM-level access:

```typescript
// infra/lib/bedrock-access-construct.ts props:
{
    allowedModels: ['anthropic.claude-*', 'amazon.nova-*']
}
```

This generates foundation model ARNs with the specific patterns instead of `*` (the inference-profile ARN remains a wildcard, since profile invocation resolves to the underlying foundation-model ARNs).

---

## 13. Configuration Reference

### Environment Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `ENABLE_BEDROCK_API` | bool | `false` | Enable the Bedrock provider |
| `BEDROCK_REGION` | string | `us-east-1` | AWS region for Bedrock API calls |
| `BEDROCK_ENDPOINT_URL` | string | *(empty)* | Custom Bedrock endpoint (for VPC endpoints or testing) |
| `BEDROCK_ALLOWED_MODELS` | CSV | *(empty)* | Comma-separated model ID glob patterns to expose (admin-level filter) |

These are the only four Bedrock-specific variables. The CDK compute stack sets `ENABLE_BEDROCK_API=true` and `BEDROCK_REGION` on the ECS container automatically.

**Persistence model (Open WebUI v0.10.x):** upstream v0.10 replaced its old persistent-config mechanism with per-key rows in the Config table. The env vars above are read in `env.py` (patch 0002) and seed the `bedrock.*` config keys (`bedrock.enable`, `bedrock.region`, `bedrock.endpoint_url`, `bedrock.allowed_models`) on first boot via `DEFAULT_CONFIG` (patch 0001). After first boot, edits made through the admin API (or the `full` target's admin panel) persist in the database and take precedence over env values.

### API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/bedrock/config` | Admin | Get Bedrock configuration |
| `POST` | `/api/v1/bedrock/config/update` | Admin | Update Bedrock configuration |
| `GET` | `/api/v1/bedrock/models` | User | List available Bedrock models |
| `POST` | `/api/v1/bedrock/chat/completions` | User | Direct Bedrock chat completion (streaming/non-streaming) |
| `POST` | `/api/chat/completions` | User | Main chat endpoint (routes to Bedrock when `owned_by == "bedrock"`) |
| `GET` | `/api/models` | User | Standard model listing — Bedrock models appear with `owned_by: "bedrock"`, filtered by the user's access grants |

Example — enable and configure the provider via the admin API (works on the default `backend` image, no UI changes needed):

```bash
curl -s -X POST "https://oui.example.com/api/v1/bedrock/config/update" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"ENABLE_BEDROCK_API": true, "BEDROCK_REGION": "us-east-1", "BEDROCK_ALLOWED_MODELS": ["us.anthropic.*", "us.amazon.nova*"]}'
```

On the opt-in `full` image target, the same settings are editable in Admin Settings → Connections → **Amazon Bedrock API**.

---

## 14. Key Design Decisions

### 1. Native boto3 Instead of OpenAI-Compatible Proxy

**Decision:** Call Bedrock Converse API directly via boto3 rather than routing through an OpenAI-compatible proxy (e.g., LiteLLM, Bedrock Access Gateway).

**Rationale:**
- Eliminates an additional service to deploy and maintain
- Uses IAM role credentials natively (no API key management)
- Full access to Bedrock-specific features (inference profiles, guardrails, usage metadata)
- Lower latency (no proxy hop)
- Simpler debugging (direct boto3 errors, not proxy-wrapped errors)

**Trade-off:** Requires maintaining the OpenAI ↔ Bedrock format translation layer.

### 2. Converse API Instead of InvokeModel

**Decision:** Use the Converse/ConverseStream APIs exclusively, not InvokeModel/InvokeModelWithResponseStream.

**Rationale:**
- Model-agnostic: same code works for Claude, Nova, Llama, Mistral, etc.
- Structured tool calling support built-in
- Consistent usage metrics across all models
- System prompts as a first-class parameter

**Trade-off:** Some model-specific features (e.g., Anthropic's extended thinking) may not be exposed through Converse.

### 3. Inference Profiles as Primary Model IDs

**Decision:** Prefer cross-region inference profile IDs (e.g., `us.anthropic.claude-*`) over base foundation model IDs.

**Rationale:**
- Required for on-demand invocation of newer models
- Provides automatic cross-region routing for higher availability
- Single ID works across all regions in the profile

**Trade-off:** Profile IDs are less human-readable than base model IDs.

### 4. Translation at the Boundary

**Decision:** Convert between OpenAI and Bedrock formats at the provider boundary, keeping the internal format as OpenAI throughout.

**Rationale:**
- Open WebUI's middleware, tools, RAG, and frontend all expect OpenAI format
- Minimizes changes to existing code
- Bedrock is treated as "just another provider" by the rest of the system

### 5. Dual-Path Chat Completion

**Decision:** Two entry points for Bedrock chat completions — a direct endpoint (`/api/v1/bedrock/chat/completions`) and the main chat endpoint (`/api/chat/completions`) via routing.

**Rationale:**
- The main endpoint goes through the full middleware pipeline (tools, RAG, filters, Socket.IO)
- The direct endpoint allows API-only usage without middleware overhead
- Both share the same underlying `invoke_bedrock_converse` / `invoke_bedrock_converse_stream` functions

### 6. Overlay + Patches Instead of a Fork

**Decision:** Distribute the integration as new files (`overlay/`) plus small attributed diffs (`patches/`) applied onto the **official** Open WebUI image at Docker build time, rather than maintaining a fork of the upstream repository.

**Rationale:**
- Upstream security and bug fixes arrive by bumping one pinned tag (currently `v0.10.2`) instead of rebasing a fork
- The full diff surface against upstream is 7 small patch files — auditable at a glance
- CI (`docker/apply-patches.sh`) proves on every build that the patches still apply cleanly to pristine upstream
- Licensing stays clean: upstream code is never vendored; only patch context lines quote it

**Trade-off:** Deep UI integration is limited — the default `backend` target can't change the frontend, which is why the admin Connections panel is an opt-in `full` target that rebuilds the UI from source.

---

## 15. CI/CD Pipeline Integration

The Bedrock integration is deployed via a CodePipeline that handles image building, CDK deployment, and post-deploy configuration.

### Pipeline Stages

```
Source (GitHub) → Deploy-Dev (CDK deploy, builds image) → Smoke Test + Approval → Deploy-Prod
```

### How Bedrock Config Flows Through the Pipeline

1. **CDK Compute Stack** sets `ENABLE_BEDROCK_API=true` and `BEDROCK_REGION` as ECS container environment variables
2. **IAM permissions** are attached to the ECS task role via the construct in `infra/lib/bedrock-access-construct.ts`
3. **Model discovery** happens at runtime — the application calls `ListInferenceProfiles` on startup using the task role's IAM credentials
4. **No API keys** are needed — boto3 uses the default credential chain (ECS task role → STS temporary credentials)

### Post-Deploy Secret Sync

The Cognito client secret must be synced to Secrets Manager after each deploy because CDK creates the secret with a random value, not the actual Cognito-generated secret:

```yaml
# In the deploy buildspec post_build phase:
POOL_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Auth \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Auth \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" --output text)
CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
    --user-pool-id $POOL_ID --client-id $CLIENT_ID \
    --query "UserPoolClient.ClientSecret" --output text)
aws secretsmanager put-secret-value \
    --secret-id open-webui/dev-cognito-client-secret --secret-string "$CLIENT_SECRET"
```

### Image Tag Strategy

The pipeline relies on deterministic asset hashes for dev → prod promotion:

```
Deploy-Dev:  cdk deploy  # builds image, pushes to CDK asset ECR with SHA256 digest
Deploy-Prod: cdk deploy  # same source tree → same asset hash → cache hit, no rebuild
```

Both environments deploy the exact same container image, referenced by its
immutable content-addressable digest. The `ComputeStack` builds the image via
a `DockerImageAsset` on this repo's overlay `Dockerfile` (target `backend` by
default, `full` opt-in) — there is no `imageTag` prop or named ECR repo to
manage. The git commit SHA is stamped into `WEBUI_BUILD_VERSION` inside the
image via the `BUILD_HASH` build arg, sourced from
`CODEBUILD_RESOLVED_SOURCE_VERSION` in the pipeline or `GIT_COMMIT` when
running `deploy.sh` locally.

---

## 16. File Inventory

### Overlay Modules (new files, copied into the image)

| File | Lines | Purpose |
|---|---|---|
| `overlay/backend/open_webui/routers/bedrock.py` | ~300 | REST API: config endpoints, model listing, chat completions (streaming + non-streaming) |
| `overlay/backend/open_webui/utils/bedrock.py` | ~650 | SDK wrapper: boto3 clients, model discovery, message conversion, Converse/ConverseStream invocation, SSE generation |
| `overlay/src/lib/apis/bedrock/index.ts` | ~110 | Frontend API client for the admin config endpoints (`full` target only) |

### Patches (modifications to upstream files)

Backend — applied in **both** image targets:

| Patch | Upstream file | Purpose | Size |
|---|---|---|---|
| `patches/backend/0001-config-bedrock-defaults.patch` | `backend/open_webui/config.py` | Seed Bedrock first-boot config defaults (`bedrock.*` keys in `DEFAULT_CONFIG`) | +22 |
| `patches/backend/0002-env-bedrock-vars.patch` | `backend/open_webui/env.py` | Read the 4 `BEDROCK_*` environment variables | +11 |
| `patches/backend/0003-main-bedrock-registration.patch` | `backend/open_webui/main.py` | Register the Bedrock router and model cache | +13 |
| `patches/backend/0004-chat-bedrock-dispatch.patch` | `backend/open_webui/utils/chat.py` | Dispatch `owned_by == 'bedrock'` completions to the Bedrock provider | +10 |
| `patches/backend/0005-models-bedrock-listing.patch` | `backend/open_webui/utils/models.py` | List Bedrock models alongside OpenAI/Ollama in the parallel model aggregation | +18/−4 |

Frontend — applied only in the opt-in **`full`** image target:

| Patch | Upstream file | Purpose | Size |
|---|---|---|---|
| `patches/frontend/0101-connections-bedrock-section.patch` | `src/lib/components/admin/Settings/Connections.svelte` | Add an "Amazon Bedrock API" section to the admin Connections panel | +74 |
| `patches/frontend/0102-constants-bedrock-base-url.patch` | `src/lib/constants.ts` | Add the `BEDROCK_API_BASE_URL` constant | +1 |

### Infrastructure (CDK TypeScript)

| File | Purpose |
|---|---|
| `infra/lib/bedrock-access-construct.ts` | IAM policy statements for Bedrock API access |
| `infra/lib/compute-stack.ts` | ECS task definition: Bedrock env vars, task-role policy attachment, `DockerImageAsset` image build |

### Build & Scripts

| File | Purpose |
|---|---|
| `Dockerfile` | Overlay build on the official `ghcr.io/open-webui/open-webui:v0.10.2` image (`backend` default target, `full` opt-in) |
| `docker/apply-patches.sh` | CI check: every patch applies cleanly to a pristine upstream checkout at the pinned tag |
| `scripts/set-model-access.sh` | Bulk grant model access by group via the REST API |
| `deploy.sh` | Local end-to-end deploy (CDK bootstrap/deploy + env management) |

---

## 17. Applying This Pattern to Other Projects

The Bedrock integration follows a repeatable pattern for adding any AWS service as a native provider to an application that uses OpenAI-compatible chat formats. Here's the generalized approach:

### Step 1: Create the SDK Wrapper (`utils/{provider}.py`)

This module owns all AWS SDK interaction. It should:

- Initialize boto3 clients with region/endpoint configurability
- Convert from your app's internal format to the AWS API format
- Convert responses back to your app's internal format
- Handle streaming by yielding SSE-formatted chunks
- Map error codes to meaningful exceptions

**Key principle:** The rest of your application never sees AWS-specific data structures. The wrapper is the only file that imports `boto3`.

### Step 2: Create the Router (`routers/{provider}.py`)

This module provides HTTP endpoints and business logic. It should:

- Expose a `generate_chat_completion()` function that the routing layer can call
- Populate provider usage metrics in the response shape your app expects
- Provide admin config endpoints
- Follow your app's existing router patterns (auth, error handling, response format)

### Step 3: Wire Into the Routing Layer

Add a routing condition based on a provider identifier:

```python
# In your chat routing function:
if model.get("owned_by") == "your-provider":
    return await generate_your_provider_completion(request, form_data, user)
```

### Step 4: Wire Into Model Aggregation

Add your provider's model listing to the parallel fetch:

```python
your_models = await fetch_your_provider_models(request)
# Ensure each model has owned_by="your-provider" for routing
```

### Step 5: Add IAM Permissions (CDK)

Create a reusable construct that generates the minimum IAM policy statements:

```typescript
// Separate discovery permissions (list/describe) from invocation permissions
// Use resource-scoped ARNs, not *
```

### Step 6: Add Configuration

Follow the pattern this sample uses on Open WebUI v0.10.x:

1. `env.py` — Read env vars with sensible defaults
2. `config.py` — Seed them into `DEFAULT_CONFIG` as per-key config rows (`{provider}.{setting}`) for first-boot persistence
3. Routers — read/write the keys via the Config model (`Config.get_many` / `Config.upsert`) so admin edits persist across restarts

### Step 7: Ship as Overlay + Patches

If you're integrating with an upstream you don't own, prefer this repo's distribution model over forking: keep new files in an `overlay/` tree, keep upstream modifications as small reviewed patch files, apply both onto the official release image in your Dockerfile, and add a CI check that the patches apply cleanly to a pristine upstream checkout. Upgrades become "bump the pinned tag, re-apply, re-emit".

### What Makes This Pattern Work

- **Format translation at the boundary** — Your app keeps its internal format; only the wrapper knows about the AWS API format
- **IAM credentials via default chain** — No API keys to manage, rotate, or leak
- **Provider routing by `owned_by`** — Clean separation; adding a provider doesn't modify existing provider code
- **Parallel model aggregation** — All providers fetched concurrently; slow providers don't block fast ones
- **Optional features degrade gracefully** — Provider disabled? Return an empty model list. Discovery call fails? Log a warning and fall back (profiles → foundation models → empty).
- **Native access control reused** — Model visibility rides the host app's existing grants system instead of a parallel one
