# Bedrock Gateway Integration Guide

How this sample connects the unmodified official Open WebUI image to Amazon
Bedrock through an Amazon Bedrock AgentCore **inference gateway**, with per-user
identity and capability-filtered model listings.

## The problem this solves

Amazon Bedrock offers an OpenAI-compatible endpoint, **`bedrock-mantle`**
(`https://bedrock-mantle.<region>.api.aws`), which Open WebUI's built-in OpenAI
connections can call. Two realities shape the design:

1. **Not every model supports every API.** On `bedrock-mantle`:
   - Most models (Qwen, DeepSeek, Mistral, gpt-oss, Gemma, …) support **Chat
     Completions** (`/v1/chat/completions`).
   - The GPT-5.x family supports **Responses** only (`/v1/responses`).
   - **Anthropic Claude** supports the **Anthropic Messages** API only
     (`/anthropic/v1/messages`) — it returns HTTP 400 on Chat Completions and
     Responses.

   A model list that ignores this puts models in the dropdown that fail the
   moment a user selects them.

2. **Identity.** Calling `bedrock-mantle` directly from the app would use one
   shared task-role identity for everyone. Routing through an AgentCore gateway
   with a **CUSTOM_JWT** authorizer lets each request carry the **logged-in
   user's own Cognito token**, so model traffic is attributable per user and
   governable with AgentCore Policy / Guardrails.

## The shape

```
Open WebUI (unmodified)                     AgentCore inference gateway
─────────────────────────                   ──────────────────────────────
 connection "gw"   (system_oauth) ─┐         CUSTOM_JWT authorizer
   api_type: chat/completions      │           trusts the Cognito user pool
   header x-models-flavor:         │─ user's ─► REQUEST interceptor (Lambda)
     chat_completions              │   JWT       • GET …/models → synthetic,
 connection "gwr"  (system_oauth) ─┤              capability-filtered list
   api_type: responses            │              (by x-models-flavor)
   header x-models-flavor:         │            • everything else passes through
     responses                     │
 pipe "gateway_anthropic" ─────────┘         bedrock-mantle inference target
   OpenAI↔Messages translation                 (GATEWAY_IAM_ROLE → SigV4)
   header x-models-flavor: messages                    │
                                                        ▼
                                              Amazon Bedrock (bedrock-mantle)
```

Three Open WebUI "lanes", one gateway, one Bedrock endpoint. All three send the
user's own OAuth token.

## Components (this repo)

### 1. The gateway — `infra/lib/gateway-stack.ts`

An `AWS::BedrockAgentCore::Gateway` (native CloudFormation resource) with:

- **Inbound auth: `CUSTOM_JWT`** whose discovery URL is the deployment's Cognito
  user pool and whose `AllowedClients` is the Open WebUI app client. The gateway
  validates the user's Cognito access token on every call.
- **Outbound auth: the gateway execution role** (`GATEWAY_IAM_ROLE`) signs
  requests to `bedrock-mantle` with SigV4. The role holds `bedrock-mantle:*`
  (note: `bedrock-mantle` is its own IAM service prefix — plain `bedrock:*` is
  not sufficient for the OpenAI-compatible endpoint).
- A **REQUEST interceptor** (see below).

The **inference target** (the `bedrock-mantle` connector) has no native
CloudFormation resource yet, so it is created by a **custom resource** —
`gateway/provisioner/index.py` — which calls
`bedrock-agentcore-control:CreateGatewayTarget` /
`DeleteGatewayTarget`. Models are addressed through the target as
`bedrock/<model-id>` (e.g. `bedrock/openai.gpt-oss-20b`).

### 2. The interceptor — `gateway/interceptor/index.py`

A REQUEST interceptor Lambda. When Open WebUI lists models
(`GET /inference/v1/models`), the interceptor **short-circuits the request** and
returns a synthetic list built from the capability matrix, choosing the list by
the `x-models-flavor` request header the connection sends
(`chat_completions` | `responses` | `messages`; default `chat_completions`).
All other paths (chat/completions, responses, messages, streaming) pass through
untouched.

Notes learned in practice:
- The gateway reports the interceptor `httpMethod` as `POST` even for the
  `GET /v1/models` listing, so the Lambda matches on **path**, not method.
- REQUEST short-circuit (not RESPONSE) is used because RESPONSE interceptors run
  in buffered mode and are **not invoked on streaming responses** — filtering on
  the request avoids interfering with chat/stream traffic entirely.
- The capability lists come from the `MODEL_CAPS` env var, populated by the
  stack from `config/model-capabilities.json`.

### 3. The capability matrix — `config/model-capabilities.json`

Which model ids work on which API — the interceptor's input, and the sample's
single source of truth for lane membership:

```json
{
  "chat_completions": ["openai.gpt-oss-20b", "qwen.qwen3-32b", "..."],
  "responses":        ["openai.gpt-5.5", "openai.gpt-oss-120b", "..."],
  "messages":         ["anthropic.claude-haiku-4-5", "anthropic.claude-sonnet-5", "..."]
}
```

Regenerate for your region/account with
[`scripts/probe-model-capabilities.py`](../scripts/probe-model-capabilities.py),
which probes every `bedrock-mantle` model against each API and excludes any that
are account-gated. Run it, commit the updated JSON, and redeploy the gateway
stack to refresh the interceptor.

### 4. The Open WebUI wiring — `pipe/seed.py`

At container start the seeder (running beside the unmodified image) waits for
the app's DB migrations and the first admin sign-in, then idempotently installs:

- **Two OpenAI connections**, both pointing at `…/inference/v1` with
  `auth_type: system_oauth` and an `x-models-flavor` header:
  - `gw` — Chat Completions lane.
  - `gwr` — Responses lane (`api_type: responses`).
- **The Claude pipe** function (`pipe/gateway_anthropic_pipe.py`), active +
  global.

Re-runs refresh the pipe code and (re)assert the connections only if absent, so
admin edits to model visibility or valves survive redeploys.

### 5. The Claude pipe — `pipe/gateway_anthropic_pipe.py`

Open WebUI's native OpenAI connections speak Chat Completions/Responses, so they
cannot drive Claude (Messages-only). This manifold pipe bridges the gap:

- **Discovery** lists `bedrock-mantle` filtered to `anthropic.*` (the
  Messages-only set). Pipe discovery runs with no user context in Open WebUI, so
  this one call uses the task role (SigV4) — read-only listing, not inference.
- **Invocation** translates OpenAI ↔ Anthropic Messages (system prompts, tool
  use/results, images, `stop_sequences`, streaming, `thinking_delta` →
  reasoning), and POSTs to the gateway `…/inference/v1/messages` as
  `bedrock/<model>` with **the user's own OAuth token** as the bearer.
- **Auth default: JWT only.** The `SIGV4_FALLBACK` valve is **off** by default —
  a user with no OAuth session (e.g. a local-password login) gets a clear error
  telling them to sign in with SSO. Turn the valve **on** to let such users fall
  back to the task role (SigV4 direct to Bedrock), which works but loses
  per-user attribution. This is deliberately opt-in.

## How a request flows

**Listing models** (dropdown): Open WebUI calls `GET …/inference/v1/models` on
each connection with its `x-models-flavor` header → the interceptor returns the
capability-verified list for that flavor → the dropdown shows only working
models (the Claude pipe lists its own models separately, via discovery).

**Chatting**:
- Chat-Completions / Responses model → the matching native connection sends the
  user's OAuth token to the gateway → `bedrock-mantle`.
- Claude model → the pipe translates to Messages and sends the user's OAuth
  token to the gateway → `bedrock-mantle`'s `/anthropic/v1/messages`.

In every case the gateway validates the user's Cognito JWT (inbound) and signs
to Bedrock with the gateway role (outbound).

## Governance you can add

Because inbound is CUSTOM_JWT with the real user identity, you can attach:

- **[AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)**
  (Cedar) — deterministic allow/deny per user, group, or model.
- **[Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html)**
  at the gateway — evaluated outside the app's context.
- **Token-limit policies** on the gateway target — bound per-request cost.

These are not enabled by default in this sample; the CUSTOM_JWT authorizer is
the foundation that makes them per-user rather than per-app.

## Operational notes

- **Adding models over time.** New `bedrock-mantle` models don't appear until
  they're in `config/model-capabilities.json`. Re-run the probe script and
  redeploy the gateway stack. (A scheduled EventBridge → probe → redeploy job is
  a reasonable extension; not included to keep the sample lean.)
- **Local-password users.** With `SIGV4_FALLBACK` off (default) they can't use
  the gateway lanes without an OAuth session. Use Cognito SSO for all users, or
  enable the fallback valve if you accept shared-identity model calls.
- **Model access control** is unchanged from stock Open WebUI: Cognito groups
  sync to Open WebUI groups; an admin sets model visibility per group in
  **Workspace → Models**.
- **Regions.** The gateway fronts `bedrock-mantle` in the deployment region;
  `bedrock-mantle` availability is a subset of Bedrock regions — verify your
  region before deploying.
