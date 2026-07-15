<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Track D — Accuracy & Reconciliation: making metered numbers trustworthy against the AWS invoice

Research session 2026-07-14/15 (overnight). Every load-bearing claim below is sourced to a URL
fetched this session or a read-only AWS CLI probe run this session (profile `prod`, us-east-1,
account 8895********). Items that could not be verified are marked `[unverified]` and repeated in
the CLAIMED/UNVERIFIED section at the bottom.

Cross-references: `04-SPIKE-FINDINGS.md` (S4/S5 usage-through-gateway, S6 Projects, S8 telemetry
gaps), `research/track-b-prior-art.md` (industry patterns P1/P2), `research/track-c-gateway-internals.md`
(no RESPONSE interceptor in streaming mode).

---

## VERIFIED

### 1. Usage-block ground truth per API shape

#### (a) OpenAI Chat Completions — streaming usage is OPT-IN via `stream_options.include_usage`

From the canonical OpenAI OpenAPI spec (`https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml`,
retrieved 2026-07-15; grep of the downloaded 2.8 MB spec):

`stream_options.include_usage` (request):

> "If set, an additional chunk will be streamed before the `data: [DONE]` message. The `usage`
> field on this chunk shows the token usage statistics for the entire request, and the `choices`
> field will always be an empty array."

`usage` on the chunk object:

> "An optional field that will only be present when you set `stream_options: {"include_usage": true}`
> in your request. When present, it contains a null value **except for the last chunk** which
> contains the token usage statistics for the entire request.
> **NOTE:** If the stream is interrupted or cancelled, you may not receive the final usage chunk
> which contains the total token usage for the request."

`choices` on the chunk object: "Can also be empty for the last chunk if you set
`stream_options: {"include_usage": true}`."

`CompletionUsage` fields: `prompt_tokens`, `completion_tokens`, `total_tokens`, plus
`completion_tokens_details` (`reasoning_tokens`, `audio_tokens`, `accepted_prediction_tokens`) and
`prompt_tokens_details.cached_tokens`.

So the final-chunk shape is: `{"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":N,"completion_tokens":M,"total_tokens":N+M}}` followed by `data: [DONE]`.
The OpenAI cookbook confirms the same contract ("the `choices` field on the last chunk will always
be an empty array `[]`"; all earlier chunks `usage: null`) — cookbook citation carried over from
Track B (retrieved 2026-07-14).

#### (b) OpenAI Responses API — usage arrives automatically on `response.completed`

Same spec (retrieved 2026-07-15): `ResponseCompletedEvent` = `{type: "response.completed",
response: <full Response object>, sequence_number}` — "Emitted when the model response is
complete." The `Response` object carries a `usage` field of type `ResponseUsage`:

> `input_tokens` (int), `input_tokens_details.cached_tokens` ("The number of tokens that were
> retrieved from the cache"), `output_tokens`, `output_tokens_details.reasoning_tokens`,
> `total_tokens` — all five required.

No opt-in flag exists or is needed on the Responses lane. Note the field-name change vs
chat-completions (`input_tokens` vs `prompt_tokens`) — a normalizer must map both.

#### (c) Anthropic Messages — `message_start` carries input, `message_delta` carries CUMULATIVE output

From `https://platform.claude.com/docs/en/build-with-claude/streaming.md` (retrieved 2026-07-14/15,
full page saved this session). Verbatim event examples:

```
event: message_start
data: {"type": "message_start", "message": {"id": "msg_1nZdL29...", "type": "message", "role": "assistant",
  "content": [], "model": "claude-opus-4-8", "stop_reason": null, "stop_sequence": null,
  "usage": {"input_tokens": 25, "output_tokens": 1}}}
...
event: message_delta
data: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": null},
  "usage": {"output_tokens": 15}}
```

And the doc's explicit warning:

> "The token counts shown in the `usage` field of the `message_delta` event are **cumulative**."

Cache fields appear in the same `usage` blocks — the doc's web-search example shows
`message_start.usage = {"input_tokens":2679,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":3}`
and a final `message_delta.usage = {"input_tokens":10682,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":510,"server_tool_use":{...}}` —
i.e. the **final `message_delta` can restate input-side counts too** (server-tool turns), so the
correct capture rule is: take `input_tokens`/cache fields from `message_start`, then overwrite with
any fields present on the last `message_delta`; `output_tokens` is always the last `message_delta`
value (cumulative), not a sum of deltas.

#### (d) Bedrock's OpenAI-compatible endpoint (`bedrock-mantle`) — docs are SILENT; behavior live-proven

- The mantle Chat Completions page (`https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions-mantle.html`,
  full page read via aws docs MCP, retrieved 2026-07-15) shows streaming examples but **never
  mentions `stream_options`, `include_usage`, or `usage` at all**. It defers: "For complete API
  details, see the OpenAI Chat Completions documentation." → **Negative finding: AWS does not
  independently document mantle's streaming-usage semantics.**
- The mantle Responses page (`.../bedrock-mantle.html`, retrieved 2026-07-15) likewise shows
  streaming but doesn't document usage events.
- **However** our own disposable spike (04-SPIKE-FINDINGS S4, run 2026-07-14 against live mantle
  through the real gateway) proved mantle follows the OpenAI contract on all three lanes:
  chat-completions emits the final usage chunk **only** with `stream_options.include_usage: true`
  (control run without the flag: no usage event); Responses emits `response.completed` with
  `response.usage`; Anthropic Messages emits `message_start.input_tokens` +
  cumulative `message_delta.output_tokens` — and the gateway's SSE passthrough does not strip any
  of it. S5 proved the REQUEST interceptor can **force** `include_usage` by rewriting the body.

### 2. Client-abort semantics — the undercount problem

The provider contract itself warns about this (OpenAI spec, verbatim above): *"If the stream is
interrupted or cancelled, you may not receive the final usage chunk."* Meanwhile the provider
bills every token generated up to cancellation (tokens are generated before they are streamed;
billing is metered at generation — see the CE evidence in §5 where billed usage exists for traffic
whose CloudWatch metrics never appeared). So any **client-side** capture point undercounts by the
full request's usage whenever the user closes the chat mid-stream.

Where each capture point sits on the abort-loss spectrum (our stack):

| Capture point | Sees usage when stream completes | Sees usage on client abort | Notes |
|---|---|---|---|
| **AWS bill (CE/CUR usage types)** | ✔ always | ✔ always (billed tokens) | Ground truth; 1K-token granularity, ≥daily lag (§5) |
| **Provider-side logs/metrics** (`AWS/BedrockMantle` CW) | ✖ unreliable | ✖ unreliable | S8: spike-night traffic never appeared even at +4 h — not a metering or reconciliation source today |
| **Gateway interceptor** | ✖ (request only) | ✖ | Track C: RESPONSE interceptors "not yet supported in streaming mode" for HTTP/inference targets — token usage cannot be observed at the gateway response hop for streaming traffic |
| **OWUI backend (pipe lane)** | ✔ (pipe parses `message_start`/`message_delta`) | ✖/partial — the pipe generator is consumed by OWUI's response task; on client disconnect the task is cancelled and the tail is never read `[mechanism unverified in OWUI source]` | Our code: `pipe/gateway_anthropic_pipe.py` L279–415 |
| **OWUI DB (`message.usage`, native lanes)** | ✔ only if the final usage chunk was requested AND received | ✖ | Chat-completions lane additionally requires OWUI's per-model `capabilities.usage` flag or interceptor body-mutation (S4 corollary + S5) |

**Quantifying the undercount:** a mid-stream abort loses that request's *entire* usage record at
every client-side point (not just the post-abort tokens), while the bill still carries
`input_tokens + output_tokens_generated_before_cancel`. Worst case per aborted call =
`estimated_input + max_tokens × output_price`. The aggregate undercount rate is proportional to
the abort rate — unmeasured for our users `[unverified]`; the reconciliation delta (§5) is how you
measure it in practice.

**Industry mitigations** (Track B, retrieved 2026-07-14): (1) **server-side capture** — LiteLLM
builds the response from chunks server-side (`stream_chunk_builder`) and prices it regardless of
client behavior; Helicone's recommended async-logging mode is the archetype; (2) **log/bill-based
reconciliation** — Langfuse explicitly calls computed cost "a best-effort estimation" and tells
you to check the provider dashboard for real billing. Consequence for us: the only abort-proof
capture points are (a) inside our own server-side streaming code path *if* it keeps reading the
upstream stream to completion after client disconnect (an explicit design decision — "drain on
disconnect"), and (b) the bill.

### 3. Token estimation for pre-request admission control

Live micro-benchmark run this session on this micro VM (ARM64, `tiktoken==0.13.0`,
`o200k_base`; command + full output in session log):

| Measure | Result |
|---|---|
| `import tiktoken` | 10.6 ms |
| First `get_encoding("o200k_base")` (cold) | **253.7 ms** |
| Encode 1 KB / 10 KB / 100 KB English | 0.05 / 0.47 / **4.6 ms** |
| Encode 10 KB Python-like code | 1.1 ms |
| RSS delta after loading encoder | **~88 MB** |
| bytes/token (English prose) | 4.50 |
| bytes/token (code) | 2.44 |
| `chars/4` heuristic error vs o200k actual | **+12.5% over** on English; **−39% under** on code |

Interpretation for a Lambda interceptor: a tokenizer is affordable — ~5 ms even at 100 KB of
prompt, but budget **256 MB+ memory** and accept ~250 ms added to cold starts (or lazy-load /
init-outside-handler). The `chars/4` (`bytes/4`) heuristic is free and matches tiktoken's own rule
of thumb — README: "On average, in practice, each token corresponds to about 4 bytes" and
"tiktoken is between 3-6x faster than a comparable open source tokeniser"
(`https://github.com/openai/tiktoken`, retrieved 2026-07-15) — but it systematically
**undercounts code ~40%** and overcounts prose ~12%.

Cross-tokenizer accuracy: tiktoken counts are only correct for OpenAI-family models. Anthropic's
own docs guidance (bundled Anthropic docs, this session): tiktoken "undercounts Claude tokens by
~15–20% on typical text, and by much more on code or non-English input." Our gateway fronts 40+
mantle models (Qwen/DeepSeek/Mistral/etc. — §4), each with its own tokenizer family, so **any
single local tokenizer is a ±15–40% estimator across the catalog**. LiteLLM's practice
(`https://docs.litellm.ai/docs/completion/token_usage`, retrieved 2026-07-14): "model-specific
tokenizers for anthropic, cohere, llama2 and openai. If an unsupported model is passed in, it'll
default to using tiktoken."

**What gateways actually do in practice** (Track B, P1 — retrieved 2026-07-14): none of the major
gateways run precise pre-request tokenization for admission. The dominant pattern is
**check-then-debit**: a cheap pre-request check of *accumulated* spend vs budget (LiteLLM: budget
check before the call against a Redis counter — "Budget checks read current spend from a cross-pod
counter in Redis… the database is reconciled in the background", enforcement error
`ExceededTokenBudget` (`https://docs.litellm.ai/docs/proxy/users`, retrieved 2026-07-15); Kong:
"cost only reflected during the next request"; Envoy: pre-request 429), accepting **bounded
overshoot** of in-flight requests ≈ `concurrent_streams × (est_input + max_tokens) × price`.

**The `max_tokens`-as-worst-case bound** is the right admission predicate for us:
`admit iff budget_remaining ≥ bytes(body)/4 × input_price + effective_max_tokens × output_price`,
where `effective_max_tokens` = request's `max_tokens` if present else a clamp the interceptor
injects (S5 proved body-rewrite works live). This makes the worst case *enforced*, not estimated:
with the clamp in place, a single admitted request can overshoot the budget by at most
`est_input_error + max_tokens × output_price` — no tokenizer needed in-path.

### 4. Price mapping — machine-readable Bedrock/mantle prices

**The AWS Price List API covers mantle usage types.** Live probe this session:

```
$ aws pricing get-products --service-code AmazonBedrock \
    --filters Type=TERM_MATCH,Field=usagetype,Value=USE1-qwen.qwen3-32b-mantle-input-tokens-standard \
    --profile prod --region us-east-1
→ sku U6WAJF7G3M4RQS3M, productFamily "Amazon Bedrock", provider "Qwen", model "Qwen3 32B",
  service_tier "standard", priceDimension: unit "1K tokens",
  "$0.00015 per 1K token for qwen.qwen3-32b-mantle-input-tokens-standard in US East (N. Virginia)",
  pricePerUnit USD 0.0001500000, effectiveDate 2026-07-01T00:00:00Z,
  offer version 20260707080509, publicationDate 2026-07-07T08:05:09Z
```

**The regional offer file is the bulk source.** Downloaded
`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/us-east-1/index.json`
(1.4 MB, version `20260707080509`, publicationDate 2026-07-07, retrieved 2026-07-15) and parsed it:

- 1,013 products total; **332 mantle usage types** across **41 models**
  (deepseek, gemma, minimax, mistral, moonshotai, nvidia, openai.gpt-oss*, qwen, writer, xai, zai).
- Usage-type kinds per mantle model: `input-tokens` / `output-tokens` / (sometimes) `cache-read-tokens`,
  each × service tier `{standard, batch, flex, priority}` — tier is part of the usage type
  (e.g. `...-mantle-input-tokens-priority`), so the price map key must be
  **(region, model, direction, tier)**, not just model.
- **Cache pricing on mantle is nearly absent**: `cache-read-tokens-*` exists for **xai.grok-4.3
  only** among mantle models; there are **zero** mantle `cache-write` usage types (Nova's
  cache-read/write SKUs are the non-mantle Converse lane). → For our lanes, a 2-column
  (input/output) price map covers everything currently billable except grok cache-reads.
- **Gap finding:** the offer file contains **no `anthropic.claude*` and no `gpt-5*` mantle SKUs**
  (claude appears in only 5 non-mantle products; gpt-5: zero) even though the live mantle catalog
  lists `anthropic.claude-*` and `openai.gpt-5.x` models (spike S8/S9, capability matrix). CE for
  this account (probe below) likewise shows no claude/gpt-5 mantle usage types this month. So:
  either those models bill under usage types not yet published in the offer file, or their SKUs
  appear only after first billed use `[unverified which]`. **Design consequence: the price map
  cannot be *fully* auto-derived from the Price List API — it needs an "unpriced model" state that
  blocks or flags spend attribution rather than silently pricing at $0.**

**LiteLLM's pattern** (fetched `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`,
retrieved 2026-07-15): one community-maintained JSON in-repo; per-model fields
`input_cost_per_token`, `output_cost_per_token`, `cache_creation_input_token_cost`,
`cache_read_input_token_cost`, limits, `supports_*` flags, `litellm_provider`, plus a
`deprecation_date` field in the sample spec; hundreds of entries including regional Bedrock
variants. It is versioned only via git — drift is handled by community PRs.

**Recommended pattern (synthesis):** versioned price map with effective-dating, refreshed from the
offer file. The offer file natively provides the primitives: file-level `version` +
`publicationDate`, and per-term `effectiveDate` (verified above — e.g. the qwen price became
effective 2026-07-01, published 2026-07-07, i.e. **repricing can be backdated by days**). Store on
every debit row: `(price_map_version, rate_used)`. A nightly job diffs
`offers/v1.0/aws/AmazonBedrock/current/<region>/index.json` against the stored version; on change,
write a new price-map version with its effectiveDate and re-rate any ledger rows in the
[effectiveDate, detection] window. That backdating window is exactly why debits must store tokens
(the invariant) and price separately (the derived value).

### 5. Reconciliation methodology — metered ledger vs Cost Explorer/CUR

**Unit sanity check — CONFIRMED: CE quantities are in THOUSANDS of tokens.** Two independent
confirmations this session: (1) the price dimension's declared `"unit": "1K tokens"` (probe above);
(2) live CE probe:

```
$ aws ce get-cost-and-usage --time-period Start=2026-07-01,End=2026-07-15 --granularity MONTHLY \
    --metrics UsageQuantity UnblendedCost --group-by Type=DIMENSION,Key=USAGE_TYPE \
    --filter '{"Dimensions":{"Key":"SERVICE","Values":["Amazon Bedrock"]}}'
→ USE1-openai.gpt-oss-120b-mantle-input-tokens-standard: UsageQuantity 4.77 "1K tokens" ($0.0007155)
  USE1-qwen.qwen3-32b-mantle-input-tokens-standard: 0.058 "1K tokens" ($0.0000087)
  … 96 Bedrock usage types this month (74 mantle + 22 non-mantle incl. Guardrail TextUnits)
```

So CE `4.77` = 4,770 tokens, and the prior observation (`12.370` qty ≈ 12,370 tokens) is confirmed
against the API's own declared unit. Reconciliation math:
`billed_tokens = UsageQuantity × 1000`; `expected_cost = UsageQuantity × rate_per_1K`.
Cross-check on the live rows: 4.77 × $0.00015 = $0.0007155 — matches UnblendedCost exactly for the
gpt-oss-120b input row.

**Data-delay envelope (all AWS-doc-sourced, retrieved 2026-07-15 via aws docs search):**

| Surface | Refresh cadence | Source |
|---|---|---|
| Billing & Cost Explorer | "refreshed at least once per day" (cadences differ between the two, causing MTD discrepancies) | docs.aws.amazon.com/cost-management/latest/userguide/differences-billing-data-cost-explorer-data.html |
| CE/Data Exports/CUR after tag backfill | "refresh your data once every 24 hours" | docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/cost-allocation-backfill.html |
| Data Exports (CUR 2.0) | "refreshes each time the source data updates, **up to three times daily**" | API_DataExports_RefreshCadence.html |
| AWS Budgets | "updated up to three times a day. Updates typically occur **between 8 to 12 hours** after the previous update"; "billing data, which Budgets uses… updated at least once per day"; explicit warning you may exceed threshold before notification | help-panel …/hp-budgets-overview-table.html, hp-budget-details.html, budgets-best-practices.html |
| Cost-allocation tags after activation | "can take up to 24 hours to appear in Cost Explorer and CUR"; "**not retroactive** — only costs incurred after activation are tagged" | docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-application-inference-profiles.html |

Consequence: the practical reconciliation lag is **~24 h (plan for 8–36 h)**; Budgets is an
8–12 h-lag alarm, never an enforcement device.

**Segmenting OUR traffic from other Bedrock traffic in the same account:**

- **Gateway resource tags do NOT flow to Bedrock usage line items.** The AgentCore gateway is a
  separate billed resource; Bedrock's mantle line items are emitted against the account with the
  usage types above and carry no gateway identity. Nothing in the Bedrock cost-management docs
  offers gateway-tag propagation. (Architecture-level negative finding; consistent with all doc
  pages read this session.)
- **Application inference profiles can't ride our path.** Bedrock docs, Projects page
  (`https://docs.aws.amazon.com/bedrock/latest/userguide/projects.md`, retrieved 2026-07-15):
  "Projects can only be used with models that use the OpenAI-compatible APIs against the
  bedrock-mantle endpoint. **If you are using the bedrock-runtime endpoint, please use Inference
  Profiles**" — and the comparison table maps Inference Profiles to Invoke/Converse on
  bedrock-runtime. Our connector targets bedrock-mantle, so inference profiles are the wrong
  primitive for this path.
- **Bedrock *Projects* ARE the native segmentation mechanism on our exact path — and they work
  through the gateway.** Projects docs: "Cost monitoring: Track spending at the project level using
  AWS tags and AWS Cost Explorer"; tags are set at project create (`POST /v1/organization/projects`
  with `tags`), then activated as cost-allocation tags; "After tag activation, you can analyze
  Amazon Bedrock costs by application inference profile [project] in… Cost Explorer… and CUR"
  (cost-mgmt page, retrieved 2026-07-15). Requests bind via the `OpenAI-Project` header
  (chat-completions/Responses) or `anthropic-workspace-id` (Messages lane) — and spike S6
  live-proved the REQUEST interceptor can inject the right header per lane, mapping JWT → project
  with zero client involvement. Up to **1,000 projects/account**; archived projects reject new
  inference and keep metrics ~30 days. **Caveats:** per-project cost segmentation lands in CE/CUR
  only at tag granularity (day × usage type × tag), tags are not retroactive, and the
  tag→CE flow was not observable same-session (S8) `[unverified-live, docs-asserted]`.

**The daily/monthly reconciliation loop (design):**

1. **Daily (run at ~T+26–30 h for day D):** ledger-sum tokens by (model, direction, tier, day)
   [and by project tag once active] → compare to
   `GetCostAndUsage(granularity=DAILY, group_by=USAGE_TYPE [,TAG])` × 1000.
   Emit per-model `drift = (billed − metered)/billed`.
2. **Monthly close (CUR 2.0 / Data Exports):** line-item-level re-check incl. any repriced SKUs
   (offer-file effectiveDate backdating, §4) and credits/RI-style adjustments; sign off the month.
3. **Alert thresholds:** industry practice tolerates a few percent — estimation-based metering is
   explicitly "best-effort" (Langfuse, Track B) and the LiteLLM/Kong/Envoy pattern accepts
   bounded overshoot; a concrete published drift SLO number does **not** exist in any doc fetched
   `[unverified — "1–5%" is folklore]`. Recommend: page at |drift| > 5% or > $X absolute; ticket at
   > 2% sustained 3 days; ignore below a 100K-token/day floor (CE's 1K-token rounding and tiny
   volumes make small-sample drift meaningless — our own account's month-to-date mantle volume is
   ~10K tokens total, where a single aborted stream would show as double-digit % drift).
4. **Attributing the delta**, in expected order of magnitude for this substrate:
   - **aborted streams** (client-side capture misses whole requests — §2) → metered < billed;
   - **unmetered lanes/paths** (calls bypassing whichever capture point feeds the ledger: native
     lanes without `include_usage`, OWUI background calls — title/tag/follow-up generation — admin
     probes, capability re-probe jobs) → metered < billed;
   - **retries** (gateway may retry interceptor failures — S7; SDK auto-retry resubmits prompts)
     → potential metered > or < billed depending on which side retried;
   - **models missing from the price map** (§4 claude/gpt-5 gap) → cost drift with token match;
   - **tier mismatch** (standard vs flex/priority usage types priced differently);
   - **CE rounding** (1K-token units, 3-decimal quantities observed).
   The reconciler should bucket the delta: match by (model,day) token counts first (isolates
   price-map errors from capture misses), then attribute the token-count residue to abort/unmetered
   using the ledger's per-request `completed|aborted` flag.

### 6. Double-count/miss matrix for our three lanes

Capture points: **A** = OWUI DB (`message.usage`, written by OWUI when the stream it *received*
contains usage), **B** = pipe-emitted usage (our manifold pipe synthesizes an OpenAI-shape usage
chunk from `message_start`/`message_delta` — `pipe/gateway_anthropic_pipe.py` L279–415, valve
`EMIT_USAGE`, verified in-repo this session), **C** = interceptor (request-only; Track C: response
interception impossible for streaming), **D** = bill (usage types).

| Lane | A: OWUI DB | B: pipe | C: interceptor | D: bill | Miss risk | Double-count risk |
|---|---|---|---|---|---|---|
| Chat-completions native | ✔ **only if** final usage chunk present — requires OWUI per-model `capabilities.usage` flag (OWUI v0.10.2 sends `stream_options.include_usage` only then; S4 corollary) **or** S5 interceptor body-mutation | n/a | request only | ✔ | abort; flag unset; OWUI-internal calls | none today (single capture point) |
| Responses native | `[unverified — whether OWUI parses `response.completed.usage` into message.usage]` | n/a | request only | ✔ | abort; OWUI parsing gap | none today |
| Anthropic pipe | ✔ (OWUI records the usage chunk **the pipe emits**) | ✔ (same numbers, emitted by our code) | request only | ✔ | abort (pipe task cancelled mid-stream `[cancellation semantics unverified]`) | **YES — A and B are the same event**: if a future billing worker reads pipe-side ledger writes AND an aggregator sums OWUI `message.usage`, one call counts twice |

**Idempotency design to make a hybrid safe:** every capture point writes to ONE ledger with a
deterministic idempotency key, and the ledger enforces first-writer-wins
(`PutItem` + `attribute_not_exists(pk)` or equivalent upsert). Key preference order:
1. **provider response id** — present on every lane and unique per completion
   (`chatcmpl-…` on chunk objects, `resp_…`/Response.id on `response.completed`, `msg_…` on
   `message_start` — all verified in the schemas/examples above); the pipe already has `msg_…` in
   hand at `message_start`;
2. fallback (id unavailable/aborted-before-start): `(owui_chat_id, owui_message_id)` — OWUI
   attaches these to the request context, making retries of the same user message idempotent;
3. last resort for interceptor-side *estimates*: gateway request id from headers
   (`x-amzn-trace-id` observed in S1).
Estimates (interceptor, pre-request) and actuals (stream tail) must be **separate ledger columns
or row-types keyed by the same id** — the actual overwrites/settles the estimate; the
reconciliation job then compares only settled actuals + unsettled estimates against the bill. That
single rule eliminates the A/B double count (same key → one row) and makes the
estimate-then-settle flow audit-friendly.

---

## CLAIMED / UNVERIFIED

Explicitly not proven this session; each item shapes the design and how to close it:

1. **Mantle honors `stream_options.include_usage` identically to OpenAI on ALL models** — proven
   live for the models probed (S4: gpt-oss family, claude-haiku via Messages lane); AWS docs never
   state it; other model families assumed. Close: extend the capability re-probe job to assert the
   usage tail per model.
2. **`AWS/BedrockMantle` CloudWatch emission criteria** — S8 observed complete absence of
   spike-night traffic at +4 h; treat the namespace as non-authoritative. `[emission criteria unknown]`
3. **Project cost-allocation-tag → CE flow end-to-end** — docs-asserted (Projects + cost-mgmt
   pages), not yet observed live (tag activation is account-level, ≤24 h lag, not retroactive).
4. **Claude/GPT-5 mantle billing SKUs** — absent from the 2026-07-07 us-east-1 offer file AND from
   this account's month-to-date CE usage types, while the models are in the mantle catalog. Unknown
   whether SKUs appear on first billed use or bill under other usage types. Close: run one
   claude-on-mantle call, wait 24–48 h, re-query CE + offer file.
5. **OWUI behavior details:** whether OWUI parses Responses-lane usage into `message.usage`; exact
   task-cancellation semantics of a pipe generator on client disconnect (drain-on-disconnect
   feasibility). Close: OWUI source read / dev-instance test.
6. **Industry drift SLO ("1–5%")** — no fetched doc publishes an acceptable-drift number;
   Langfuse's "best-effort estimation" is the closest published stance. Our 2%/5% thresholds are
   recommendations, not citations.
7. **Abort *rate* for our user base** — unmeasured; the reconciliation delta is the measurement.
8. **Archived-project carrying cost** — none documented or expected (S8 note). 
9. **Anthropic tokenizer-mismatch magnitude ("~15–20%")** — from Anthropic's bundled docs guidance
   this session, not independently benchmarked against a Claude `count_tokens` call.

---

## Decision-relevant synthesis (Track D conclusions)

1. **Capture at the stream tail, server-side, on every lane** — force `include_usage` at the
   interceptor (S5) so the tail always exists; parse it wherever our code touches the stream; keep
   reading upstream to completion on client disconnect where we own the reader (pipe), because the
   provider bills what it generated whether or not anyone read it.
2. **Admission control needs no tokenizer**: `bytes/4` for input + interceptor-clamped
   `max_tokens` as the worst-case bound, check-then-debit like every production gateway. If
   estimation quality ever matters, tiktoken costs ~5 ms/100KB + 88 MB + 250 ms cold — affordable
   but only correct for OpenAI-family models.
3. **Price map = versioned snapshot of the AWS offer file** keyed (region, model, direction, tier),
   with effective-dating, an *unpriced-model* state (claude/gpt-5 gap is real), and per-debit
   `price_map_version` stamping. Grok is the only mantle model with cache-read pricing today.
4. **Reconcile daily against CE usage types (×1000 = tokens; unit live-confirmed), monthly against
   CUR**, at ~T+26 h; segment our traffic via **Bedrock Projects + activated cost-allocation tags**
   (interceptor-injected per-user/team headers) — gateway tags don't reach Bedrock line items and
   application inference profiles don't apply to the mantle path.
5. **One ledger, idempotency key = provider response id (fallback OWUI chat/message id),
   estimate-then-settle rows** — that closes the pipe-lane double-count and gives the reconciler
   clean "settled vs unsettled vs billed" buckets whose residue *is* the abort/unmetered rate.
