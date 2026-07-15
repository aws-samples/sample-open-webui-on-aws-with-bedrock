<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# 04 — Spike Findings (disposable live probes, 2026-07-14)

One evening of disposable, net-new probes against live AWS to close the unknowns that
gated the design in [`02-DESIGN.md`](02-DESIGN.md). Everything below was **built new,
tagged `Purpose=disposable-metering-spike`, and torn down to zero residuals the same
session** (teardown log at the bottom). The existing dev gateway and the Dev/Prod
Open WebUI fleet were not touched.

All account IDs are redacted (`8895********`); the spike ran in `us-east-1`.

## What was stood up (and later destroyed)

| Resource | Name | Purpose |
|---|---|---|
| Cognito user pool + client + user | `spike-metering-pool` | mint a real end-user JWT (mirrors the sample's `system_oauth` flow) |
| DynamoDB table | `spike-metering-quota` | quota-read latency measurement inside the interceptor |
| Lambda | `spike-metering-interceptor` | instrumented REQUEST interceptor (log/deny/mutate/ddb modes) |
| IAM roles ×2 | `spike-metering-*-role` | Lambda exec + gateway exec (`bedrock-mantle:*`, `lambda:InvokeFunction`) |
| AgentCore gateway | `spike-metering-gw-…` | CUSTOM_JWT (spike pool), REQUEST interceptor, `passRequestHeaders: true` |
| Inference target | `bedrock` (connector `bedrock-mantle`) | same shape as the sample's gateway stack |
| Bedrock mantle Project | `spike-metering-proj` (`proj_nqyr********`) | per-caller attribution probe (archived at teardown) |

## Findings

### S1. The REQUEST interceptor receives the user's raw JWT — per-user identity is fully available in-path — **CONFIRMED**

With `passRequestHeaders: true` (the sample's existing setting), the interceptor event's
`http.gatewayRequest.headers` contains the **verbatim `Authorization: Bearer <access token>`**
of the calling user, on every path (`/v1/models`, `/v1/chat/completions`, `/v1/messages`).
The gateway has already *validated* the token (bad issuer/no token were rejected upstream in
earlier substrate work), so the Lambda can decode claims (`sub`, `username`, `cognito:groups`)
without re-verification for attribution purposes (verify signature anyway if the value gates
enforcement — cheap with a cached JWKS).

There is **no parsed-claims context field** (nothing like API Gateway's
`requestContext.authorizer`) — the Lambda decodes the JWT itself. Full header set observed:
`authorization`, `content-type`, `accept`, `host`, `user-agent`, `x-amzn-trace-id`,
`x-forwarded-for`, TLS metadata. Custom client headers (e.g. `x-models-flavor`, and our test
headers) pass through untouched.

The **body arrives base64-encoded** and is the full JSON request (model, messages,
`max_tokens`, `stream`, `stream_options`), so per-request input-size estimation and
`max_tokens` clamping are possible in-path.

### S2. Interceptor short-circuit with HTTP 429 + OpenAI-shaped error body — **CONFIRMED** (the enforcement primitive)

`transformedGatewayResponse: { statusCode: 429, contentType: "application/json", body: <b64> }`
returned end-to-end in **~216 ms total** (vs ~40 ms TTFB for passthrough — the delta is one
Lambda invoke + response serialization; warm-path interceptor execution alone is **1.6–2.5 ms**).
The client sees:

```json
{"error": {"message": "Monthly token quota exceeded. Usage resets 2026-08-01. …",
           "type": "quota_exceeded", "code": "quota_exceeded"}}
```

Status codes are not restricted to 200 (docs impose no enumeration; 429 live-proven).
This is the pre-request **deny** mechanism: same primitive the sample already uses for the
`/models` short-circuit, now proven for arbitrary status + body on a chat path.

### S3. Quota lookup inside the interceptor costs ~3–5 ms warm — **CONFIRMED** (enforcement check is latency-cheap)

A DynamoDB `GetItem` (eventually-consistent, on-demand table) inside the interceptor measured
**133 ms cold / 3.4–4.7 ms warm** (5 runs). End-to-end TTFB of streamed chat calls with the
lookup in path was indistinguishable from baseline (~39–40 ms). A pre-request quota check
against DynamoDB is affordable on every call.

### S4. The `usage` block survives gateway streaming on all three lanes — **CONFIRMED** (the metering-capture prerequisite)

Streaming SSE through the gateway (CUSTOM_JWT, connector target), observed at the client:

| Lane | Mechanism | Usage event observed through gateway |
|---|---|---|
| Chat Completions | requires `stream_options: {"include_usage": true}` | final chunk `"usage":{"completion_tokens":25,"prompt_tokens":73,…}` ✔ |
| Chat Completions *without* the flag | — | **no usage event** (5 lines vs 6) ✔ control |
| Responses | automatic | `response.completed` → `response.usage {input_tokens, output_tokens, …}` ✔ (direct-to-mantle probe; gateway passthrough is transformation-free per docs + S4 chat evidence) |
| Anthropic Messages | automatic | `message_start` → `input_tokens`; `message_delta` → cumulative `output_tokens` ✔ through gateway |

The gateway's stream passthrough does not strip or reorder the usage tail.
**Corollary (from repo evidence, not the spike):** Open WebUI v0.10.2 only sends
`stream_options.include_usage` when the model's `capabilities.usage` flag is set
(`src/lib/components/chat/Chat.svelte` @v0.10.2), so the chat-completions lane's usage
tail is **opt-in per model** — the metering design must either set that capability flag
on seeded models or inject the flag at the interceptor (S5).

### S5. The interceptor can MUTATE the request body — force `include_usage` on every call — **CONFIRMED**

Interceptor decoded the base64 body, set `stream_options={"include_usage": true}` on a
request that omitted it, returned `transformedGatewayRequest.body` — and the client received
the usage chunk. This removes any dependency on Open WebUI's per-model `usage` capability
flag for server-side capture paths, and proves body-rewrite (e.g. `max_tokens` clamping)
works on the live path.

### S6. Interceptor header injection works — and Bedrock **Projects** give native per-caller attribution on our exact path — **CONFIRMED** (the headline finding)

`bedrock-mantle` has an OpenAI-style org/projects API (Track A found the docs; the spike
proved the path):

- `POST /v1/organization/projects` created `proj_nqyr********` (SigV4, no console needed).
- Direct chat call with header `OpenAI-Project: <project ARN or proj_ id>` → 200; with a
  **nonexistent** project → `404 "Project '…' does not exist"` — the header is validated
  server-side, i.e. genuinely processed, not ignored.
- **Through the gateway**, the client's `OpenAI-Project` header passes through to mantle
  (nonexistent-project 404 reproduced end-to-end through the gateway).
- **The interceptor can inject the header itself** (`transformedGatewayRequest.headers`):
  injected nonexistent project → 404 (proof of injection reaching mantle); injected real
  project → 200, streamed with usage. **This is the per-user/per-team attribution
  mechanism: interceptor maps JWT → project, injects the header; zero client involvement.**
- Lane nuance: the Anthropic Messages path rejects `OpenAI-Project` with
  `400 "The `openai-project` header is not supported for this API format. Use `anthropic-workspace-id` instead."`
  — and **`anthropic-workspace-id: proj_…` works** (200; bad id → the same 404 validation).
  Unknown headers (`x-project`, `anthropic-project`) are ignored, so the interceptor must
  inject the right header per path.

### S7. Interceptor failure is **fail-closed** — **CONFIRMED**

A deliberately raised exception in the interceptor produced a client-visible
`HTTP 400` whose body wraps the Lambda error (`errorMessage`, `errorType`, `stackTrace` —
with the gateway's `exceptionLevel` defaulted; expect terser output when unset/PROD).
The request **never reached the model**. Design consequence: the interceptor Lambda is a
hard dependency of every chat call once installed — its availability posture, and any
"fail-open on quota-store outage" behavior, must be implemented *inside* the Lambda
(catch errors → allow) because the platform default is closed. Retries: docs say the
gateway may retry interceptor failures; idempotency required.

### S8. Telemetry lag/coverage caveats — **PARTIALLY CONFIRMED / NEGATIVE results that shape the design**

- `AWS/BedrockMantle` CloudWatch metrics never showed **any** of the spike-night traffic —
  not the project-tagged calls, not the untagged (default-project) calls, not the
  `openai.gpt-oss-20b` / `anthropic.claude-haiku-4-5` model dimensions — re-checked
  **4 h after** the calls (00:03 UTC vs ~20:00–22:05 UTC traffic). The namespace still
  contains only the 2026-07-09 series (`openai.gpt-5.4/5.5`, `anthropic.claude-fable-5`,
  `Project=default`) — while Cost Explorer showed **complete** per-model mantle usage
  types for 07-09 (38+ models). Despite the docs describing the namespace as covering all
  three mantle API shapes, treat it today as **unreliable for completeness** (unknown
  emission criteria, multi-hour-to-never lag observed): fine for coarse alarms when data
  is present, **not** a metering source and **not** a reconciliation source. Billing
  usage types are the complete record. `[emission criteria unverified; observed: tonight's
  traffic absent at +4 h]`
- The spike gateway emitted **only `InboundAuthorizationSuccess`** to
  `AWS/Bedrock-AgentCore` despite real streamed inference passing through — confirming
  Track C's live finding that inference data-plane traffic currently produces no
  Invocations/Latency/token metrics at the gateway.
- Project **cost-allocation-tag → Cost Explorer** flow could not be observed same-session
  (CE lags ~24 h and tag activation is account-level). Docs assert it; marked
  `[unverified-live]` in the design.

### S9. Responses-API model drift — **OBSERVED** (substrate hygiene, not metering)

`openai.gpt-5.5` / `gpt-5.5-2026-04-23` / `gpt-5.6-terra` returned
`400 "does not support the '/v1/responses' API"` on 2026-07-14, though gpt-5.5 worked on
2026-07-09 (memory + capability matrix). `openai.gpt-oss-20b` still works on `/v1/responses`
(200 + `response.completed` usage). The mantle catalog now lists `gpt-5.6-sol/luna/terra`.
The capability matrix (`config/model-capabilities.json`) is stale within days — the design's
scheduled re-probe job is not optional hygiene, it is load-bearing.

## Latency summary (warm paths, us-east-1, n=5 each)

| Path | Measure |
|---|---|
| Gateway streamed chat TTFB, interceptor passthrough | 39–40 ms |
| Gateway streamed chat TTFB, interceptor + DDB GetItem | 39–41 ms (no measurable delta) |
| Interceptor Lambda execution (passthrough) | 1.6–2.5 ms |
| DDB GetItem inside Lambda | 3.4–4.7 ms warm; 133 ms cold |
| 429 short-circuit total | ~216 ms |
| Interceptor cold start (first invoke) | ~135 ms Lambda duration |

## Teardown confirmation (same session, 2026-07-14 ~23:30 UTC)

Deleted and verified-gone, in order: gateway target → gateway (`ResourceNotFoundException`
on re-get), Lambda (`ResourceNotFoundException`), DynamoDB table (`DELETING` →
`ResourceNotFoundException`), both IAM roles (`NoSuchEntity`), Cognito pool
(`ResourceNotFoundException`), Lambda log group (describe returns `[]`). The mantle Project
was **archived** via `POST …/projects/{id}/archive` (200; projects have no delete —
archived projects reject new inference; `[unverified]` whether an archived project has any
carrying cost — none is documented or expected). `list_gateways` contains no `spike*`
entry; the dev gateway `owui-models-jwt-…` remains `READY` and untouched. Billable
residuals: **zero** (the only artifacts are CloudWatch log events already emitted and
~$0.01 of Bedrock/Lambda/DDB usage during the spike).
