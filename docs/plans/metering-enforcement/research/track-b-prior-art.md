# Track B — Prior art: per-user budgets + hard cutoffs in the LLM-gateway ecosystem

Research date: 2026-07-14. All URLs fetched this session unless marked otherwise. GitHub issue evidence gathered via authenticated `gh search issues` / `gh issue view` against `BerriAI/litellm` (commands quoted inline).

Substrate constraints this research is scored against (given, verified elsewhere):
- Managed AgentCore inference gateway (CUSTOM_JWT/Cognito) → `bedrock-mantle` connector. We control a REQUEST interceptor Lambda invoked on every call; we have **no code in the response stream**.
- Traffic is streaming SSE. Zero UI modification allowed. AWS-native strongly preferred.

---

## VERIFIED FINDINGS

### 1. LiteLLM proxy — the richest (and most battle-scarred) prior art

Sources: https://docs.litellm.ai/docs/proxy/users, https://docs.litellm.ai/docs/proxy/cost_tracking, https://docs.litellm.ai/docs/proxy/virtual_keys, https://docs.litellm.ai/docs/proxy/db_deadlocks, https://docs.litellm.ai/docs/proxy/prod (all retrieved 2026-07-14).

- **Where it meters:** in-proxy. It is the request path, sees every response, computes cost per request (`x-litellm-response-cost` response header).
- **Price map:** community-maintained `model_prices_and_context_window.json` in the repo; `completion_cost()` = tokens × per-token prices; `register_model()` to override; provider-specific tier metadata (incl. "Bedrock service tiers") applied automatically (cost_tracking doc). Tokenizer fallback: `token_counter` uses "model-specific tokenizers for anthropic, cohere, llama2 and openai. If an unsupported model is passed in, it'll default to using tiktoken" (https://docs.litellm.ai/docs/completion/token_usage, retrieved 2026-07-14).
- **When it debits:** post-response, **asynchronously and batched**. Production guidance: `proxy_batch_write_at: 60` (batch spend writes every 60s) (prod doc). At 10+ instances, direct UPDATE/UPSERTs deadlock, so spend updates go through an in-memory queue → shared **Redis transaction buffer** (`use_redis_transaction_buffer: true`); one instance takes a distributed lock and flushes to Postgres periodically (db_deadlocks doc). Implication stated by the doc structure itself: budget enforcement lags spend by up to the flush interval.
- **Where/when it enforces:** **pre-request check** during auth. Budget exceeded → HTTP **401** `{"detail": "Authentication Error, ExceededTokenBudget: Current spend for token: 7.2e-05; Max Budget for Token: 2e-07"}` (users doc; note: some paths return 429 `BudgetExceededError` — both shapes appear in docs+issues). Budget levels: global, team, team-member, internal user, key, end-customer; `budget_duration` resets (`30s/30m/30h/30d`). **No mid-stream cutoff anywhere — enforcement is always next-call.**
- **Multi-instance consistency:** cross-pod **Redis counter is the hot-path source of truth**, background DB reconciliation; rate-limit counters use "async increments synced every 0.01 seconds" with documented drift "at most 10 requests at high-traffic (100 RPS across 3 instances)" (users doc).
- **Fail-open vs fail-closed:** default is fail-closed on DB unavailability (requests error); explicit opt-in fail-open flag `allow_requests_on_db_unavailable: true` ("Request will be allowed" on Prisma/Httpx errors), recommended only for private-VPC deployments (prod doc). Opposite strict mode also exists: `fail_closed_budget_enforcement: true` = "every budgeted request validates spend against the authoritative database before being admitted", 503 if neither Redis nor DB can verify (users doc).
- **Streaming usage capture:** builds the complete response from chunks (`stream_chunk_builder`) and prices it; the streaming docs do not document `stream_options` handling explicitly (https://docs.litellm.ai/docs/completion/stream, retrieved 2026-07-14 — negative finding: no explicit streaming-usage contract documented).
- **Pre-charge/reservation mechanism exists** (`reserve_budget_for_request` pre-charges estimated max cost via Redis `INCRBYFLOAT` before the LLM call, `reconcile_budget_reservation` adjusts to actual after) — verified via issue #30460's root-cause analysis quoting `litellm/proxy/spend_tracking/budget_reservation.py` (see below). The dedicated docs page 404s (`docs.litellm.ai/docs/proxy/budget_reservations`, checked 2026-07-14) — the mechanism is real but under-documented.
- **Spend-attribution spoofing caveat:** clients "can artificially underreport spend by passing the `user` parameter in requests rather than relying on their API key's assigned user_id" (cost_tracking doc). (Our JWT-derived identity avoids this class.)

**Documented failure modes (GitHub, `gh issue view <n> --repo BerriAI/litellm`, all read 2026-07-14):**
- **#26672** (open): budget enforcement **silently bypassed** for key+user `max_budget` in v1.82.3 — spend tracked, blocking never fires; regression vs v1.81.0. Lesson: the cutoff path needs its own canary test; it regresses independently of metering.
- **#30460** (open, v1.85.3, 2-pod EKS + ElastiCache): Redis spend counters **inflate above DB** (Redis=50 vs DB=14) → false `429 BudgetExceededError`. Three compounding causes incl. pre-charge reservations never reconciled on ElastiCache timeout and budget reset not invalidating per-pod caches which re-seed the counter. Lesson: estimate-and-reconcile leaks under partial failure; single-writer atomic counters are safer than counter+cache hierarchies.
- **#27639** (closed): `reserve_budget_for_request()` leaks Redis counters → "phantom BudgetExceededError", users blocked at ~$0 real spend, cycling every ~4 min on 4 replicas.
- **#27735** (open, v1.84.0): virtual-key `BudgetExceededError` from **stale** spend counter while `/key/info` shows spend below budget — 13/15 failures were pre-request blocks that never reached the LLM.
- **#28979** (open): spend-reporting endpoint transiently inflates by $25–65 for 5–15 min → external budget scripts false-trigger. Lesson: read-your-writes consistency of the spend store matters for anything that acts on it.
- **#31078 / #27923** (open): budget-exceeded also blocks `GET /v1/models` → clients' model discovery breaks. **Directly relevant to us: our interceptor already short-circuits GET /v1/models; a budget block must exempt it or OWUI model lists break.**
- **#27300, #27481, #16057**: budget **resets** are a recurring bug source (budget ignored after reset; tag budgets never reset — "blocked permanently after first overage"; team-member budgets not reset).
- **#30776** (closed): admin spend mutations via `/key/update` bypass the cross-pod counter — enforcement ignores the change. Lesson: exactly one write path to the counter.
- **#31292** (open): global proxy budget ignores `budget_duration` (hardcoded trailing-30-day). 
- No open issues found matching "parallel requests budget overshoot" as such (`gh search issues --repo BerriAI/litellm "parallel requests budget"` → `[]`, 2026-07-14); the overshoot concern is addressed by their reservation mechanism, whose failure modes are #30460/#27639 above.

### 2. Portkey

Source: https://portkey.ai/docs/product/administration/enforce-budget-and-rate-limit and https://portkey.ai/docs/product/ai-gateway/virtual-keys/budget-limits (retrieved 2026-07-14).

- Budgets on **API keys / integrations / workspaces**; **cost (min $1) or token (min 100)** budgets; **alert thresholds** notify before exhaustion while "the API key continues to function until the full budget limit is reached".
- Resets: none, weekly (Sun 00:00 UTC), or monthly (1st 00:00 UTC).
- Enforcement: budgets "automatically prevent further usage when limits are reached"; on exhaustion the key "automatically expires" → subsequent calls fail (i.e., **next-call blocking**). **Negative finding:** docs do not publish the error status/shape, pre- vs post-request semantics, streaming capture, or accuracy/latency of accounting. Enterprise-plan-only feature.

### 3. Helicone

Sources: https://docs.helicone.ai/features/advanced-usage/custom-rate-limits, https://docs.helicone.ai/references/latency-affect, https://docs.helicone.ai/references/availability (retrieved 2026-07-14).

- **Meters in-proxy** (Cloudflare Workers on the edge); async-logging mode also exists and is their **recommended lower-risk production option** ("With asynchronous logging, Helicone stays out of your critical path") — but async mode observes only; blocking features need the proxy in path.
- **Rate-limit-by-cost**: request header `Helicone-RateLimit-Policy: "[quota];w=[time_window];u=[unit];s=[segment]"`; `u=cents` gives spend limits (e.g. `500;w=3600;u=cents` = $5/hour); segments: global, per-user (`Helicone-User-Id`), per-property. Window min 60s. Exceed → **429** with `Helicone-RateLimit-*` headers. Counters in "Cloudflare's key-value data store".
- **Fail-open by design**: "No matter what happens, we gracefully fallback to just proxying the LLM request, ensuring uninterrupted service."
- **Negative findings:** no documented hard *budget* (only windowed cost rate-limits), no documented accuracy guarantees, no documented streaming-usage mechanics; token-based limits listed as forthcoming.

### 4. Cloudflare AI Gateway

Sources: https://developers.cloudflare.com/ai-gateway/features/, .../features/spend-limits/, .../configuration/rate-limiting/, .../features/unified-billing/ (retrieved 2026-07-14).

- **Does do budgets**: "Spend Limits" = "Per-user or per-team budgets using custom metadata", daily/weekly/monthly (rolling or fixed) windows, "Automatic request blocking when budget is exceeded".
- Cost = computed from "token usage and model pricing", explicitly "**a best-effort estimation**" — they tell you to check the provider dashboard for real billing.
- **Enforcement is pre-request**: rules evaluated "before sending a request to the provider"; exceed → **429**; options = block until window reset **or fallback-route to a cheaper model** (Dynamic Routes). No mid-stream cutoff documented.
- Plain rate limiting is **requests-only and per-gateway** (no per-user, no tokens).
- **Unified billing** (prepaid credits across OpenAI/Anthropic/Google/xAI/Groq, 5% fee): **explicitly NOT a hard cutoff** — "your credit balance may go negative... Cloudflare will charge the payment method on file".
- **Negative finding:** streaming usage capture mechanics undocumented.

### 5. OpenRouter — prepaid credits = hard cutoff by design

Sources: https://openrouter.ai/docs/api-reference/limits, https://openrouter.ai/docs/api-reference/streaming (retrieved 2026-07-14).

- Out of credits → **HTTP 402**; **negative balance blocks everything, even free models**, until topped up. Per-key caps: `limit`, `limit_remaining`, `limit_reset`.
- **Mid-stream exhaustion**: "the error arrives as an SSE event with `finish_reason: "error"` instead of an HTTP 402, since the 200 status was already sent." — i.e., even the hardest-cutoff vendor lets the current stream terminate via an in-band error event; it does not kill mid-token silently.
- Streaming usage: "Final chunk includes usage stats" (`chunk.usage`).
- Client cancellation: for supported providers, aborting the stream "immediately stops model processing and billing"; otherwise you are billed for the complete response.

### 6. Envoy AI Gateway + Kong AI Gateway — token-aware rate limiting

Envoy: https://aigateway.envoyproxy.io/docs/capabilities/traffic/usage-based-ratelimiting (retrieved 2026-07-14).
- Extracts `InputToken/CachedInputToken/OutputToken/TotalToken` from OpenAI-schema responses via `llmRequestCosts` → filter metadata; limits via `BackendTrafficPolicy` (Global Rate Limit API); counters in **Redis (deployed separately)**; **pre-request check**: "if the limit would be exceeded, the request is rejected with a 429". Set request-cost 0 so only tokens count. **Negative finding:** streaming capture timing not documented.

Kong AI Rate Limiting Advanced: https://developer.konghq.com/plugins/ai-rate-limiting-advanced/ (retrieved 2026-07-14).
- "Uses the token data returned by the LLM provider"; **the current request is admitted blind — "The cost for the AI Proxy... is only reflected during the next request"** (explicit next-call semantics; bounded overshoot of one request accepted).
- Four strategies: `total_tokens`, `prompt_tokens`, `completion_tokens`, `cost` (v3.8+) = `(prompt_tokens × input_cost + completion_tokens × output_cost) / 1,000,000` with rates from AI Proxy config (a per-model price map).
- Sync strategies: `local` (fast, inaccurate across nodes), `cluster` (DB-backed, slow), `redis` (recommended balance). Exceed → **429** `{"message": "API rate limit exceeded for provider ..."}`.
- **Negative finding:** streaming token counting not documented on the plugin or AI Proxy reference pages (checked https://developer.konghq.com/plugins/ai-proxy/reference/, retrieved 2026-07-14; `log_statistics` "if... supported by the driver" adds token metrics to logs).

### 7. AWS-native prior art

- **aws-samples/genai-gateway** (https://github.com/aws-samples/genai-gateway, retrieved 2026-07-14): AWS's official "budgets for Bedrock" answer is **LiteLLM on ECS-Fargate/EKS + ElastiCache Redis + RDS + ALB/CloudFront + Secrets Manager**, budgets/limits at "user, team, and api key level", Okta OAuth JWT. i.e., AWS ships the pattern in §1 rather than a native service feature.
- **aws-samples/bedrock-access-gateway** (https://github.com/aws-samples/bedrock-access-gateway, retrieved 2026-07-14): OpenAI-compatible Bedrock proxy with **no cost tracking, no budgets, no per-user metering** (negative finding); points at application inference profiles + CloudWatch for usage.
- **Application inference profiles** (https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-application-inference-profiles.html via aws___search_documentation, retrieved 2026-07-14): per-tenant cost **attribution**, not enforcement — "Application inference profiles deliver aggregated billed dollars to AWS Cost Explorer and CUR... The finest grain is per usage type per day; **they do not produce per-request cost**. For per-prompt token detail, use Per-request metadata tagging with your model invocation logs." AWS blog (https://aws.amazon.com/blogs/machine-learning/track-allocate-and-manage-your-generative-ai-cost-and-usage-with-amazon-bedrock/, retrieved 2026-07-14) confirms: AWS Budgets alerts + Cost Anomaly Detection + CloudWatch alarms = **"alerting mechanisms only, not hard cutoffs"**.
  - **Critical negative finding for our path:** the mantle quotas page states "Custom inference profile quotas, batch inference quotas, and Provisioned Throughput allocations apply only to the `bedrock-runtime` endpoint and **are not exposed on the `bedrock-mantle` endpoint**" (https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-mantle.html, retrieved 2026-07-14). Per-tenant AIP tricks do not obviously carry over to our mantle connector.
- **bedrock-mantle's own quota engine is in-path prior art** (same page): admission = input tokens + `max_tokens` (or model max) **pre-reserved** against input-TPM, 429 if it would exceed; output TPM is enforced **during generation — "If the quota is reached during generation, generation stops and the response is returned with a finish reason indicating the cutoff"**; "any unused portion of the initial input-token reservation... is replenished". No RPM quotas on mantle. This is a live reserve-then-reconcile implementation, per-account (not per-user).
- **AWS Budgets Actions** (https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-controls.html, retrieved 2026-07-14): can auto-apply a **deny IAM policy or SCP** at a cost threshold — a real AWS-native hard cutoff, but account/tag-scoped, billing-data latency (hours–a day), not per-Cognito-user; best as a backstop kill-switch, not per-user budgets.
- **DynamoDB atomic counters + condition expressions** (https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html and https://aws.amazon.com/blogs/database/implement-resource-counters-with-amazon-dynamodb/, retrieved 2026-07-14): `UpdateItem` with `ADD` is serialized server-side ("no race condition concerns with making multiple simultaneous calls"); add a `ConditionExpression` to enforce a threshold atomically at no extra cost; documented ambiguity on 500-errors (over/under-count; choose retry policy by which direction you can tolerate). This is the AWS-native equivalent of everyone else's Redis counter — with enforcement-at-write built in.
- **API Gateway usage plans** (CDK docs via aws___search_documentation, retrieved 2026-07-14): throttling + quotas are **per API key in units of REQUESTS**, not tokens or dollars — negative finding for LLM budgets; also not in our data path.

---

## CLAIMED / UNVERIFIED

- Portkey enforcement internals (status code, error body, pre-request vs async, streaming capture): **not published**; "key expires at limit" implies next-call blocking [unverified].
- Helicone rate-limit-by-cost requiring proxy mode (not async logging): strongly implied by architecture (async mode cannot block) but not stated [unverified inference].
- Envoy/Kong streaming token capture (parse of final usage chunk vs post-stream aggregation; whether they inject `stream_options.include_usage`): undocumented in the pages fetched [unverified].
- Whether an application-inference-profile ARN can be passed as `model` to bedrock-mantle chat-completions at all [unverified; quotas doc implies no].
- Cloudflare Spend Limits GA status and streaming-cost capture [unverified].
- LiteLLM `stream_options: {"include_usage": true}` injection behavior on proxy pass-through [unverified in docs; observed only via issues].
- OpenAI streaming-usage contract (background fact, verified against OpenAI cookbook: with `stream_options={"include_usage": true}`, a final extra chunk has `usage` populated and "the `choices` field on the last chunk will always be an empty array `[]`"; all earlier chunks have `usage: null` — https://developers.openai.com/cookbook/examples/how_to_stream_completions, retrieved 2026-07-14). Whether **bedrock-mantle** honors `stream_options.include_usage` identically is [unverified — needs the Track-A/spike probe; the mantle chat-completions doc page shows streaming but never mentions stream_options].

---

## SYNTHESIS — patterns that survive our constraints

Constraint recap: managed gateway, we hold only a REQUEST interceptor; cannot see/modify the response stream; zero UI change; AWS-native.

**P1. Pre-request admission check + post-hoc async debit ("check-then-debit", next-call enforcement).** The industry-dominant pattern (LiteLLM pre-request auth check; Cloudflare "before sending a request to the provider"; Envoy 429 pre-request; Kong "cost only reflected during the next request"). Fits us exactly: the REQUEST interceptor short-circuits with a 429/402-style error when the user's balance is exhausted; usage is debited after the fact from telemetry. Accepted industry cost: **bounded overshoot of the in-flight request(s)** (≈ concurrent_streams × max_tokens × price).

**P2. Usage capture OUT of the response path (async log/telemetry ingestion), not in-stream parsing.** Helicone's recommended async-logging mode is the archetype; Kong/Envoy read provider-returned usage after the response. For us: derive usage from whatever the managed path emits (gateway/mantle logs, CloudWatch metrics, invocation logs) rather than trying to interpose on SSE. This is the only option available to us anyway — treat it as the design center, not a compromise.

**P3. Price-map cost computation + scheduled reconciliation against the bill.** Everyone prices as tokens × static per-model map (LiteLLM `model_prices_and_context_window.json` is the de-facto industry map; Kong `cost` strategy; Cloudflare "best-effort estimation"). Nobody claims billing-grade accuracy; LiteLLM's own docs prescribe reconciliation (time-range alignment, token-category compare incl. cache, then ingestion-vs-formula-vs-price-map triage). AWS-native reconciliation anchor: CUR/Cost Explorer with cost-allocation tags (daily grain).

**P4. Single serialized counter with threshold-at-write (DynamoDB conditional atomic counter) instead of Redis counter+cache hierarchies.** LiteLLM's multi-layer (per-pod cache → Redis → DB) design produced its worst bugs (#30460, #27639, #27735). DynamoDB `UpdateItem ADD` + `ConditionExpression` gives serialized increments and atomic enforcement in one call, no separate rate-limit infra, and matches our multi-instance Lambda interceptor. One write path only (LiteLLM #30776 lesson).

**P5. (Optional hardening) Reserve-then-reconcile for concurrent-stream overshoot.** Prior art: bedrock-mantle's own quota engine (reserve input+max_tokens, replenish unused) and LiteLLM `reserve_budget_for_request`. Powerful but the leakiest pattern in practice (phantom-charge issues above) — only worth it if P1's bounded overshoot (cap `max_tokens`, cap parallel streams) is unacceptable.

**P6. Layered backstops, alert-before-block.** Per-user soft threshold → notify (Portkey/Cloudflare alert thresholds); per-user hard cap → block next call; account-level AWS Budgets (+ optionally Budgets Actions deny-policy) as the catastrophic backstop. Exempt `GET /v1/models` and other free/control endpoints from the block (LiteLLM #31078/#27923 broke clients by blocking discovery).

**Industry consensus, stated explicitly:**
- **Mid-stream cutoff is rare and non-default.** LiteLLM, Kong, Envoy, Cloudflare, Portkey all block the *next* call only. The only mid-stream behaviors found: OpenRouter terminates an in-flight stream with an in-band SSE error event when credits die mid-stream, and bedrock-mantle stops generation when *output-TPM quota* is hit. No product kills the socket from a control plane; with no code in the stream we lose nothing the industry actually does.
- **Fail-open on metering-infra failure is the availability default** (Helicone "gracefully fallback to just proxying"; LiteLLM's opt-in `allow_requests_on_db_unavailable` for private deployments), with **fail-closed as an explicit strict/compliance mode** (LiteLLM `fail_closed_budget_enforcement` → 503). LiteLLM's out-of-box default is fail-closed on DB loss, which its own docs steer private-VPC users away from. Recommended posture for us: fail-open on counter-store errors, fail-closed only on a positively-confirmed exceeded budget, plus a canary that proves the block path still fires (LiteLLM #26672 shows enforcement silently regressing while metering keeps working).
