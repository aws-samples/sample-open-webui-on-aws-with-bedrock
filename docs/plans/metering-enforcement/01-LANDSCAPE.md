<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# 01 — Landscape: what exists today (verified 2026-07-14)

Two halves: **(a)** AWS-native primitives for LLM metering/enforcement, each verified
against current documentation this session (URL + retrieval date) and, where possible,
against live account telemetry or a disposable probe; **(b)** how the LLM-gateway
ecosystem already solves per-user budgets + hard cutoffs, from primary docs and issue
trackers. Full evidence with quotes lives in [`research/`](research/)
(track A = AWS primitives, B = prior art, C = gateway internals, D = reconciliation);
this file is the decision-relevant digest. "Our path" = AgentCore inference gateway
(CUSTOM_JWT/Cognito) → `bedrock-mantle` connector → Bedrock's OpenAI-compatible endpoint,
streaming SSE. Verified vs claimed is marked per row; anything `[unverified]` was not
proven this session.

---

## (a) AWS-native primitives

### Reaches our path — VERIFIED

| Primitive | What it gives us | Key evidence |
|---|---|---|
| **Bedrock Projects** (mantle-native workload boundary) | Per-request attribution via `OpenAI-Project: proj_…` header (chat/completions + responses) and `anthropic-workspace-id` (messages — discovered via live probe); project ARN validated server-side; AWS tags on the project flow to billing records → Cost Explorer + CUR 2.0 (after cost-allocation activation); `Project` dimension in CloudWatch; IAM-scopeable (`bedrock-mantle:CreateInference` per project ARN); ≤1000 projects/account; every account has a `default` project that absorbs unheaded traffic | docs.aws.amazon.com/bedrock/latest/userguide/projects.html + cost-mgmt-projects.html (retrieved 2026-07-14) [A]; created/archived a project + header validation live-proven through the gateway, incl. interceptor header-injection [S6] |
| **`AWS/BedrockMantle` CloudWatch namespace** | `Inferences`, `InferenceClientErrors`, `TotalInputTokens/TotalOutputTokens` (aggregate) and `InputTokens/OutputTokens` (one datum per inference) with dimensions `Project` × `Model` — documented as covering all three mantle API shapes | monitoring-mantle-metrics.html (retrieved 2026-07-14) [A]; live: namespace exists with 07-09 series — **but** spike-night traffic (tagged and untagged) never appeared even 4 h later, and only 3 of 38+ billed models ever show [S8] → treat as best-effort; not a metering or reconciliation source today |
| **Per-model mantle usage types in CE/CUR** | The invoice itself, per model per day: `{REGION}-{model}-mantle-{input\|output}-tokens-{tier}` (unit = **1K tokens**; `12.370` qty ↔ ~12,370 tokens cross-checked against same-day probes) | live CE queries 2026-07-09/14 [recon + A]; CUR 2.0 carries the same line items + project tags per docs (cost-mgmt-projects.html) [tag-flow live-unverified — CE lags ~24 h] |
| **mantle service quotas** (account-level) | Per-model input-TPM and output-TPM, adjustable via Support case; **no RPM quotas**; admission pre-reserves `input + max_tokens` (429 if exceeded), output-TPM can stop generation mid-stream — AWS's own reserve-then-reconcile implementation, account-scoped | quotas-mantle.html (retrieved 2026-07-14) [A]; live: 8 quotas listed incl. Claude Opus 4.8 output-TPM 4M |
| **Gateway REQUEST interceptor** | Sees every call on all three lanes with the user's raw JWT (`passRequestHeaders: true`); can rewrite body+headers (`transformedGatewayRequest`) and short-circuit any status code with custom body (`transformedGatewayResponse`); fail-closed on crash (platform behavior) | gateway-interceptors-types docs (retrieved 2026-07-14) [C]; 429 short-circuit, body mutation (`include_usage` injection), header injection, DDB-read-in-path ~3–5 ms, crash⇒400-blocked all live-proven [S1–S7] |
| **AWS Price List API mantle SKUs** | Machine-readable per-1K-token USD for mantle usage types (`GetProducts ServiceCode=AmazonBedrock`, attributes carry `usagetype: …-mantle-…`) → generated, versioned price map | live GetProducts probe 2026-07-14 (35 mantle SKUs on first page, incl. `-flex` tier) |
| **AWS Budgets + Budget Actions** | Monthly/quarterly/annual cost alerts (data lag 8–24 h class, evaluated ~3×/day); Actions can auto-attach a deny IAM policy (e.g. `Deny bedrock-mantle:CreateInference` on the gateway role) — account-grade kill-switch, not per-user; **no daily-period actions** | budgets-controls.html + help-panel budgets overview (retrieved 2026-07-14) [A] |
| **Cost Anomaly Detection** | ML anomaly alerts on cost, can key on cost-allocation (project) tags; ~3 evaluations/day on data that itself lags ≤24 h; documented caveat: third-party Marketplace-billed line items (e.g. Anthropic-entity charges) may be excluded — coverage of mantle Claude spend `[unverified]` | manage-ad.html (retrieved 2026-07-14) [A] |
| **CloudTrail data events for mantle** | Audit record per inference (`bedrock-mantle.amazonaws.com` / `CreateInference`, incl. `/anthropic/v1/messages`), carries project ARN + request params — **no token counts** (`responseElements: null`); extra data-event cost | logging-cloudtrail-mantle.html (retrieved 2026-07-14) [A] |
| **DynamoDB atomic counters** | `UpdateItem ADD` serialized server-side + `ConditionExpression` = threshold-at-write; the AWS-native equivalent of everyone's Redis counter, with enforcement built into the write | WorkingWithItems.html + resource-counters blog (retrieved 2026-07-14) [B]; 3.4–4.7 ms warm from inside the interceptor [S3] |

### Does NOT reach our path — VERIFIED NEGATIVE (record of absence)

| Primitive | Why not | Evidence |
|---|---|---|
| **Application Inference Profiles** | bedrock-runtime (`InvokeModel`/`Converse`) only; docs literally say "For workloads using Responses and Chat Completions on the `bedrock-mantle` endpoint, **use Projects instead**"; profile ARNs don't fit the gateway target's plain-string model routing; also only daily-grain dollars, never per-request | cost-mgmt-application-inference-profiles.html + API_ModelEntry.html (retrieved 2026-07-14) [A] |
| **Bedrock model-invocation logging** | "Calls made through other endpoints, such as … the `bedrock-mantle` endpoint, are **not currently captured** by invocation logging" | model-invocation-logging.html (retrieved 2026-07-14) [A] |
| **Per-request metadata tagging (billing)** | "Per-request metadata tagging is not available on the `bedrock-mantle` endpoint, so per-prompt cost detail is not currently available" | cost-mgmt-projects.html (retrieved 2026-07-14) [A] |
| **IAM-principal billing attribution** | "Support for `bedrock-mantle` APIs is **coming soon**" — not today | cost-mgmt-iam-principal-tracking.html (retrieved 2026-07-14) [A] |
| **Gateway RESPONSE interceptor on streams** | "HTTP targets support both REQUEST and RESPONSE interceptors **in buffered mode** … **Interceptors are not yet supported in streaming mode**" — the 2026-07-09 finding, now explicit in docs; MCP targets *did* gain streaming response interceptors, inference targets did not | gateway-interceptors-types (retrieved 2026-07-14) [C] |
| **Gateway token/usage telemetry** | No token metric, no per-caller dimension on any gateway metric; token metrics in `AWS/Bedrock-AgentCore` are Memory-only; vended logs = APPLICATION_LOGS with MCP-oriented content (no tokens, no JWT sub), no USAGE_LOGS for gateways; live: our inference gateway emits only `InboundAuthorizationSuccess/Failure` — not even request counts | observability-gateway-metrics + live probes (2026-07-14) [C][S8] |
| **AgentCore Policy (Cedar) as quota engine** | Principal = JWT claims as tags (incl. `cognito:groups`) but action space is MCP-tool-shaped; **no usage/counter/dynamic state** — only interceptor-injected context attributes; no documented Cedar entities for inference operations | policy-* docs (retrieved 2026-07-14) [A][C] |
| **Gateway "token limit policies"** | Referenced by the inference-connector docs ("configure a token limit policy on your gateway target") and June-2026 release notes, but the linked page 404s and the control-plane API model contains **no such field** — telegraphed, not shipped; re-check before build | gateway-target-inference-connector.html + botocore model grep (2026-07-14) [A][C] |
| **API Gateway usage plans** | Request-count units, not tokens/dollars — and not in our data path anyway | CDK/APIGW docs (retrieved 2026-07-14) [B] |

### Newer 2025–2026 items checked

CloudWatch **Generative AI Observability** (GA Oct 2025: token/latency views for AgentCore
components — observability only, per-component not per-end-user); AgentCore **Harness**
`maxTokens` (Harness agents only, not gateway traffic); AgentCore **Payments** spend
limits (agent-pays-third-party, not user metering); no gateway usage-plan/API-key-metering
feature exists in docs or the API model. [A, all retrieved 2026-07-14]

---

## (b) LLM-gateway prior art (what they meter, where they enforce, what they get wrong)

Full quotes + issue links in [`research/track-b-prior-art.md`](research/track-b-prior-art.md).

| Product | Meters | Debits | Enforces | Fail posture | Streaming capture |
|---|---|---|---|---|---|
| **LiteLLM** (docs.litellm.ai, retrieved 2026-07-14) | in-proxy, per-request cost header; community price map JSON; tiktoken fallback | async + batched (60 s), Redis cross-pod counter → Postgres flush | **pre-request check** at auth; exceeded → 401/429; budgets at global/team/user/key/customer levels; `budget_duration` resets | default fail-closed on DB loss; opt-in `allow_requests_on_db_unavailable`; opposite strict flag too | builds response from chunks; `stream_options` contract undocumented |
| **Portkey** (portkey.ai/docs, 2026-07-14) | virtual-key/workspace budgets ($ min 1 or tokens min 100) | vendor-internal | key "expires" at limit → next-call block; alert thresholds before | `[unverified]` | `[unverified]` |
| **Helicone** (docs.helicone.ai, 2026-07-14) | edge proxy (or async logging = observe-only) | KV counters | windowed **rate-limit-by-cost** (`u=cents`), 429 + headers; min 60 s window; no true budget | **fail-open by design** ("gracefully fallback to just proxying") | `[unverified]` |
| **Cloudflare AI Gateway** (developers.cloudflare.com, 2026-07-14) | in-proxy; cost = "best-effort estimation" (their words) | vendor-internal | **pre-request** Spend Limits per user/team metadata; daily/weekly/monthly; 429 or fallback-route to cheaper model; unified billing explicitly NOT a hard stop (balance can go negative) | `[unverified]` | `[unverified]` |
| **OpenRouter** (openrouter.ai/docs, 2026-07-14) | prepaid credits | at response | **hard by construction**: 402 when out; negative balance blocks even free models; mid-stream exhaustion → in-band SSE `finish_reason:"error"` (stream ends gracefully, not killed) | fail-closed by construction | final chunk carries usage |
| **Envoy AI Gateway** (aigateway.envoyproxy.io, 2026-07-14) | extracts token counts from OpenAI-schema responses | Redis global rate-limit counters | **pre-request** token-budget check → 429 | `[unverified]` | post-response extraction; timing undocumented |
| **Kong AI Gateway** (developer.konghq.com, 2026-07-14) | provider-returned token data; `cost` strategy = tokens × per-model price config | local/cluster/redis strategies | explicit **next-call**: "cost … is only reflected during the next request"; 429 | `[unverified]` | undocumented |
| **AWS's own answer** (github.com/aws-samples/genai-gateway, 2026-07-14) | = LiteLLM on ECS/EKS + Redis + RDS | (LiteLLM) | (LiteLLM) | (LiteLLM) | (LiteLLM) |

**Documented failure modes worth designing around** (LiteLLM issue tracker, read 2026-07-14):
enforcement silently regressing while metering still works (#26672 → ship an enforcement
canary); Redis counter+cache hierarchies inflating/leaking → phantom blocks at $0 spend
(#30460, #27639, #27735 → one serialized counter, one write path); budget blocks breaking
`GET /v1/models` and killing client model discovery (#31078/#27923 → exempt discovery
paths); budget resets as a recurring bug class (#27300/#27481/#16057 → windows keyed by
date, no reset job); admin spend mutations bypassing the counter (#30776 → single write
path); client-supplied `user` params enabling attribution spoofing (their docs → derive
identity from the validated JWT only).

**Industry consensus, extracted:**
1. **Pre-request check + async post-debit, block the *next* call** — universal (LiteLLM,
   Cloudflare, Kong, Envoy). Bounded overage of in-flight requests is the accepted cost.
2. **Nobody cuts streams mid-flight from a control plane.** The only mid-stream behaviors
   found anywhere: OpenRouter ends the stream with an in-band error event, and
   bedrock-mantle's own output-TPM quota stops generation. Losing mid-stream cutoff to
   the managed gateway costs us nothing the industry actually does.
3. **Cost numbers are estimates; reconciliation is the accuracy story.** Cloudflare calls
   its own numbers "best-effort"; LiteLLM prescribes bill reconciliation. Finance-grade
   truth comes from the provider's billing pipeline (for us: CE/CUR usage types + project
   tags).
4. **Fail-open on metering-infra failure is the availability default** (Helicone
   explicitly; LiteLLM's private-VPC guidance), with fail-closed as an explicit
   strict/compliance mode.
5. **Reserve-then-reconcile exists** (LiteLLM budget reservations; mantle's own quota
   engine) **but leaks under partial failure** — adopt only if bounded next-call overage
   is unacceptable.

---

## What this landscape decides

The intersection of (a) and (b) — a managed gateway we cannot put response-stream code
into, an interceptor that sees every request with identity, provider-emitted usage that
survives to the app tier, invoice-grade per-model usage types, and an industry consensus
of pre-check/post-debit — is what makes the recommended architecture in
[`02-DESIGN.md`](02-DESIGN.md) a hybrid: **enforce pre-request at the gateway
interceptor, meter from the stream's own usage block at the app tier, attribute per-team
with Projects, reconcile nightly against CE/CUR, backstop with quotas + Budgets.**
