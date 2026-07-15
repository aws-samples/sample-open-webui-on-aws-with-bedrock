# Track C — AgentCore Gateway interception + telemetry capabilities (verified against current docs)

Research date: **2026-07-14**. All URLs below were fetched this session (via the AWS docs MCP tools, which return verbatim page text). Live probes ran read-only `aws` CLI against account 8895********, region us-east-1, profile `prod`. Our internal notes being re-verified date from 2026-07-09/10.

Substrate (given): gateway `owui-models-jwt-b***********` (CUSTOM_JWT/Cognito) → inference **connector** target `bedrock` (connectorId `bedrock-mantle`, GATEWAY_IAM_ROLE) + provider target `responses-scoped`; REQUEST interceptor Lambda `owui-gw-models-filter` with `passRequestHeaders: true`; streaming SSE traffic.

---

## TL;DR (decision-relevant)

1. **Inference targets use the HTTP interceptor payload shape** (`http` key, base64 bodies) — not the MCP shape. REQUEST interceptor can mutate **body + headers** (not path, not method) and can short-circuit with **statusCode + headers + contentType + body**.
2. **RESPONSE interceptors do NOT run for streaming responses on HTTP/inference targets** — "buffered mode" only, "not yet supported in streaming mode" (exact quote below). MCP targets DID gain streaming response interceptors (per-event invocation), but that is the MCP lane, not ours.
3. **No token-usage metric and no per-caller dimension exists for gateway inference traffic** in CloudWatch. `InputTokenUsage`/`OutputTokenUsage`/`TokenCount` are Memory-only (confirmed live). Worse (live finding): our inference gateway's data-plane requests currently emit **no Invocations/Latency metrics at all** — only `InboundAuthorizationSuccess/Failure`.
4. **Gateway vended logs = APPLICATION_LOGS only** (CW Logs/S3/Firehose). Documented content is MCP-oriented (request/response bodies for *MCP operations*); no token counts, no JWT-sub field in the documented schema. No USAGE_LOGS for gateway (that's Runtime).
5. **A "token limit policy" for inference targets is referenced by the docs but has no config surface** in the current API model or docs (searched shapes + docs; the "Gateway policies" link 404s). Do not design around it existing today.
6. **Cedar Policy on gateways is MCP-tool-shaped** (principal=OAuthUser w/ JWT-claim tags incl. custom claims; action=`Target___tool`; context = `context.input` only). No documented Cedar entities for inference operations or model IDs; no external/dynamic data lookup (only interceptor-injected context attributes). Deny = HTTP 403 `AuthorizationError`.

---

## VERIFIED

### Q1a. REQUEST interceptor input/output schema (inference lane)

**Docs pages (retrieved 2026-07-14):**
- Overview: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html
- Types (payload contracts): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.md
- Configuration: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-configuration.md
- API ref: https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GatewayInterceptorConfiguration.html

Key facts (all quoted/paraphrased from the Types page unless noted):

- **Which payload shape applies to us:** "HTTP targets use a different interceptor payload structure than MCP targets. HTTP targets (AgentCore Runtime and passthrough) and **inference targets all use this `http` payload structure**. Inference is a separate target type that happens to share the HTTP interceptor payload shape. The payload uses an `http` key instead of an `mcp` key. Request and response bodies are **base64-encoded strings** rather than parsed JSON objects."
- **Input (REQUEST, http shape):**
  ```json
  { "interceptorInputVersion": "1.0",
    "http": { "gatewayRequest": {
        "path": "/my-target-name/invocations",
        "httpMethod": "POST",
        "headers": { "Content-Type": "application/json", "Authorization": "<bearer_token>" },
        "body": "<base64_encoded_body>" } } }
  ```
  - `headers` included **only if `passRequestHeaders: true`** (our gateway has it true — live `get-gateway` output below). The example explicitly shows the **`Authorization: <bearer_token>` header is passed** — i.e., the interceptor receives the user's validated JWT raw.
  - **No parsed identity/claims context field exists** (nothing like API GW `requestContext.authorizer`). The Lambda must decode the JWT itself. (Negative finding; see below.)
  - **Lambda client context** (HTTP targets): `GATEWAY_ARN`, `GATEWAY_ACCOUNT_ID`, `REQUEST_ID` always present; `SOURCE_IP` optional. ("Client context" section of Types page.)
  - `httpMethod` "is included for informational purposes but is **read-only**".
- **Output (REQUEST, http shape):** `transformedGatewayRequest: { headers, body }` and/or `transformedGatewayResponse: { contentType, statusCode, headers, body }`.
  - Quote: "If the output contains a `transformedGatewayResponse`, the gateway returns that response immediately **without calling the target**." And: "**The RESPONSE interceptor does not run after a short-circuit**" (HTTP-target semantics — note this **differs from MCP targets**, where "the RESPONSE interceptor will still be invoked" after a REQUEST-interceptor short-circuit).
  - **Short-circuit `statusCode`:** it is a free integer field in the contract; **docs place no restriction on allowed values** (no enumeration). Custom **headers CAN be set** on the short-circuit response (field present in the output schema). `contentType` settable. We have only live-proven 200; other codes (e.g. 429) are undocumented-but-unrestricted → see UNVERIFIED.
  - `body` must be base64-encoded.

**Q1d — what can be mutated:** `transformedGatewayRequest` allows **`headers` and `body`** (both shown in the documented output example; header injection explicitly demonstrated with `X-Custom-Header`). **`httpMethod` cannot be modified** ("The `httpMethod` is not included in the output because it cannot be modified"). **`path` does not appear in `transformedGatewayRequest`** — path rewrite by the interceptor is not part of the contract. So: **rewriting the model id or injecting `stream_options.include_usage` into the JSON body is supported** (decode base64 → edit JSON → re-encode), as is header injection; path rewriting is not.

**API/CFN surface (verified against live botocore 1.43.48 service model, dumped locally):**
```
GatewayInterceptorConfiguration { interceptor: {lambda:{arn}}, interceptionPoints: [REQUEST|RESPONSE] (1..2), inputConfiguration? }
InterceptorInputConfiguration { passRequestHeaders (required bool), payloadFilter?: { exclude: [{field: "RESPONSE_BODY"}] } }
InterceptorPayloadExclusion enum = ["RESPONSE_BODY"]   ← the ONLY excludable field today
```
CFN: `AWS::BedrockAgentCore::Gateway` `GatewayInterceptorConfiguration` (interceptionPoints / interceptor.lambda.arn / inputConfiguration.passRequestHeaders) exists — https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-bedrockagentcore-gateway-gatewayinterceptorconfiguration.html (referenced from CDK docs fetched this session). Devguide confirms: max **one REQUEST and one RESPONSE interceptor per gateway**; Lambda only.

**Live confirmation of our config** (`aws bedrock-agentcore-control get-gateway --gateway-identifier owui-models-jwt-b***********`, 2026-07-14):
```json
"interceptorConfigurations": [{ "interceptor": {"lambda": {"arn": "...:function:owui-gw-models-filter"}},
  "interceptionPoints": ["REQUEST"], "inputConfiguration": {"passRequestHeaders": true} }]
```
(Also `get-gateway-target` returns `targetConfiguration: {"SDK_UNKNOWN_MEMBER": {"name": "inference"}}` under the AWS CLI v2.34.58 baked into this VM — the CLI's model predates inference targets; the runtime API knows them. Use up-to-date boto3 for automation.)

### Q1b. RESPONSE interceptors and STREAMING — the big one

**Exact wording, Types page (gateway-interceptors-types.md, retrieved 2026-07-14):**

> "HTTP targets support both REQUEST and RESPONSE interceptors in **buffered mode**, in which the gateway buffers the full response before invoking the response interceptor. **Interceptors are not yet supported in streaming mode.**"

and, from the MCP-vs-HTTP comparison table on the same page:

> "Response interceptor | [MCP target] Supported | [HTTP target] **Supported in buffered mode (not yet supported in streaming mode)**"

So for our inference lanes (streaming SSE chat/completions, Responses, /v1/messages): **a RESPONSE interceptor will not see streamed responses.** This re-confirms our 2026-07-09/10 note; the docs wording is now explicit ("not yet supported in streaming mode") rather than implied.

Two nuances the current docs added since our notes:

1. **MCP targets now DO support streaming response interceptors** (section "Response interceptors with streaming enabled"): when `protocolConfiguration.mcp.streamingConfiguration.enableResponseStreaming: true`, the response interceptor "is invoked multiple times during a streaming response — once per eligible event", with `gatewayResponse.isStreamingResponse: true`; only the first invocation may override `headers`/`statusCode`; it is *not* invoked for `notifications/progress`, `notifications/message`, or pings. This is MCP-payload-shaped and gated on JSON-RPC `id`-bearing events — **it does not apply to inference targets**, but it shows the service direction (streaming interception exists in one lane already).
2. **Buffered-mode response interceptors ARE supported for HTTP/inference targets** — with a 6 MB Lambda-payload caveat: "Lambda synchronous invocation has a 6 MB payload limit... If a target returns a large body — **which is common with inference models** — the base64-encoded payload can exceed this limit." Mitigation: `payloadFilter.exclude: [{field: "RESPONSE_BODY"}]`, after which the response interceptor still sees `statusCode`/`contentType`/`headers` (body `null`) and can inject headers/override status. (Types + Configuration pages.)

Implication for metering: **token usage cannot be observed at the gateway response hop for streaming inference traffic.** For non-streaming calls only, a buffered RESPONSE interceptor could parse `usage` from the body.

### Q1c. Interceptor timeout / latency budget / fail-open-vs-closed

- **No timeout, latency budget, or fail-open/fail-closed statement exists in the docs.** (Checked overview, types, configuration, permissions, examples pages + quotas page search.) Negative finding.
- The only adjacent statement (overview page, Security best practices): "Implement **idempotent** Lambda functions for your interceptors. **The gateway may retry requests to interceptor Lambda functions in case of failures or timeouts.** Ensure your interceptor logic can handle duplicate invocations safely..." — retries exist; the terminal behavior on persistent interceptor failure is undocumented.
- AWS blog "Apply fine-grained access control with Bedrock AgentCore Gateway interceptors" (https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/, retrieved 2026-07-14 via search chunk) shows interceptor duration surfacing in CloudWatch (their sample: "4.47 milliseconds average") — anecdotal, not a budget.

### Q2. CloudWatch metrics (`AWS/Bedrock-AgentCore`)

**Docs** — gateway observability page (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.md, retrieved 2026-07-14):
- Invocation metrics: `Invocations`, `Throttles` ("throttled (status code 429) by the service"), `SystemErrors` (5xx), `UserErrors` (4xx except 429), `Latency` (**time to first response token**), `Duration` (to final token), `TargetExecutionTime`. Dimensions: **Operation** (e.g. InvokeGateway), **Protocol** (e.g. MCP), **Method** (MCP op), **Resource** (gateway ARN), **Name** (tool name). Usage metric: `TargetType` (count per target type). Batched at 1-minute intervals.
- **No per-caller/per-user dimension is documented. No token metric is documented for gateway.** (Negative finding, confirmed.)
- Policy metrics page (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-policy-metrics.html): `AllowDecisions`, `DenyDecisions`, `TotalMismatchedPolicies`, `PolicyMismatch`, `MismatchErrors`, `DeterminingPolicies`, `NoDeterminingPolicies` (+ Invocations/Latency/SystemErrors/UserErrors) with dims **OperationName** (`AuthorizeAction`|`PartiallyAuthorizeActions`), **PolicyEngine**, **Policy**, **TargetResource** (gateway id), **ToolName**, **Mode** (`LOG_ONLY`|`ENFORCE`).

**Live probe** (`aws cloudwatch list-metrics --namespace AWS/Bedrock-AgentCore`, us-east-1, 2026-07-14 — 41 distinct metric names):
- `InputTokenUsage` / `OutputTokenUsage` exist **only** with `Operation=Consolidation` on **memory** ARNs (+StrategyId/StrategyType). `TokenCount` exists only with `Operation=LongTermMemoryProcessing` on memory ARNs. **Confirmed: token metrics are Memory-only — none attach to gateways.**
- `ActiveStreamingConnections`, `InboundStreamingBytesProcessed`, `OutboundStreamingBytesProcessed` exist only on **runtime** ARNs (WebSocket/command-shell operations) — none for our gateway.
- Gateway data-plane dims observed live: `{Operation=InvokeGateway, Protocol=HTTP, Method=InvokeHttp, Name=InvokeHttp, Resource=<gateway ARN>}` — but the only gateway emitting these is `uni-agent-gw` (HTTP passthrough target; 24 invocations on 2026-07-14). MCP connector target dims observed: `{ConnectorId=web-search, TargetType=CONNECTOR, Operation, Protocol=MCP}`.
- **Our inference gateway (`owui-models-jwt`) emits ONLY `InboundAuthorizationSuccess` (dims: ResourceId=gateway ARN) and `InboundAuthorizationFailure` (+ExceptionType=UnauthorizedInboundTokenException).** InboundAuthorizationSuccess by day: 7/09: 367, 7/10: 70, 7/11: 2, 7/13: 56, 7/14: 66 — so it IS taking authenticated traffic. Yet `Invocations` with `Resource=<our ARN>` (with/without Protocol=HTTP), and every `ConnectorId=bedrock-mantle` combination probed, returned **zero datapoints** over 7/01–7/14. `get-metric-statistics` probes quoted in the working log; e.g.:
  ```
  aws cloudwatch get-metric-statistics --metric-name Invocations \
    --dimensions Name=Operation,Value=InvokeGateway Name=Resource,Value=arn:...gateway/owui-models-jwt-b*********** \
    --start-time 2026-07-07 ... → Datapoints: []
  ```
  Two candidate explanations (recorded, not resolved): (a) interceptor-short-circuited requests (`GET /v1/models`, the bulk of pipe traffic) don't reach a target and may not emit data-plane invocation metrics; (b) inference-target invocations emit under an Operation/Protocol dimension combination that isn't discoverable via list-metrics because it has had no recent traffic, or aren't instrumented yet. Either way: **today we cannot even count our inference requests from CW metrics, let alone attribute per-user or per-token.**
- Aside (footnote): list-metrics in this account also returns memory-ARN metrics from *other* AWS accounts (e.g. 474938223294, 323928187009) — likely the March-2026 "Observability: Cross-Account Monitoring" feature surfacing shared telemetry; irrelevant to our design but noted to avoid future confusion.

### Q3. Gateway log delivery / vended logs / traces

**Docs** (observability-gateway-metrics.md + observability-configure.md, both retrieved 2026-07-14):
- Gateway supports vended log delivery to **CloudWatch Logs, S3, or Firehose**. Default CW log group: `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{gateway_id}` (custom groups must start `/aws/vendedlogs/`). Console flow for gateways offers **Log type = APPLICATION_LOGS** (the runtime console flow is the one that mentions APPLICATION_LOGS only too; **USAGE_LOGS appears in docs/live only for runtime resources**). Traces: "Configure tracing delivery to CloudWatch" section covers delivery-source `logType TRACES` for resources; gateway spans are documented (below).
- Documented APPLICATION_LOGS content for gateways: "Start and completion of gateway requests processing; Error messages for Target configurations; MCP Requests with missing or incorrect authorization headers; MCP Requests with incorrect request parameters (tools, method)" plus "request and response bodies as part of your Vended Logs integration **when any of the MCP Operations are performed** on the Gateway." Sample log fields: `resource_arn, event_timestamp, body{isError, log, requestBody|responseBody, id}, account_id, request_id, trace_id, span_id`. **No token counts. No caller identity (JWT sub) field.** The wording is MCP-operation-scoped; nothing documents per-request records for inference operations. (Negative findings.)
- **Vended spans (OTEL):** documented only for MCP operations — List Tools / Call Tool / Search Tools; `kind:SERVER` + `kind:CLIENT` (target execution) spans with attributes `aws.operation.name, aws.resource.arn, aws.request.id, gateway.id, latency_ms, overhead_latency_ms, tool.name, http.response.status_code, jsonrpc.error.code, ...` — **no token-usage attributes, no inference-operation spans documented.** (Negative finding for "does the gateway emit spans with token usage for inference targets": **no, not documented.**)
- Delivery-source mechanics: the SDK/CLI flow is standard CW vended-logs (`put-delivery-source` with the resource ARN, `put-delivery-destination`, `create-delivery`) per observability-configure.md ("Step 1: Create delivery source for logs... Step 2: ...for traces").

**Live probe:** `aws logs describe-delivery-sources` shows APPLICATION_LOGS/TRACES/USAGE_LOGS sources for **runtime** and **memory** ARNs only (USAGE_LOGS only on runtimes, e.g. `claude_code_agui`), none for any gateway; `describe-log-groups --log-group-name-prefix /aws/vendedlogs/bedrock-agentcore` shows memory/runtime/workload-identity groups only. So gateway log delivery is real but **not yet enabled in our account**; enabling it is a write action (needs approval) and, per docs, would yield MCP-oriented APPLICATION_LOGS of uncertain value for the inference lane.

### Q4. Policy (Cedar) on gateways for inference traffic

**Docs (all retrieved 2026-07-14):**
- Attachment: `GatewayPolicyEngineConfiguration { arn, mode: LOG_ONLY | ENFORCE }` — https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GatewayPolicyEngineConfiguration.html (LOG_ONLY "evaluates... adds traces... does not enforce"; ENFORCE "allows or denies agent operations"). Default in CDK is LOG_ONLY.
- Entities (policy-authorization-flow.md + policy-schema-constraints.md):
  - **Principal** `AgentCore::OAuthUser::"<JWT sub>"`; **all JWT claims stored as tags** on the entity (`username`, `iss`, `scope`, plus custom claims — AWS blog example matches `principal.getTag("cognito:groups") like "*policyholders*"`, so **cognito:groups matching works**). IAM-auth gateways get `AgentCore::IamEntity` (id = STS ARN, no tags).
  - **Action** `AgentCore::Action::"Target___tool"` — "Each MCP tool becomes an action... All tool actions inherit from CallTool → Mcp hierarchy". The Cedar schema "is automatically generated from the Gateway's **MCP tool manifest**".
  - **Resource** `AgentCore::Gateway::"<arn>"`.
  - **Context**: "Only available context is `context.input`" (typed tool input params; string→String, integer→Long, number→Decimal, boolean→Bool). "`context.output` can only be used with guardrails" (June 2026: AgentCore Policy supports Bedrock Guardrails evaluating tool outputs/inputs at the gateway — release notes + gateway-guardrails.html).
  - Semantics: **default deny once an engine is attached** ("deny-by-default"; you need a baseline `permit`), forbid wins.
- **Inference traffic:** there is **no documented Cedar action/entity for inference operations, no model-id attribute, no request-property (max_tokens etc.) context** for the `/inference` lanes. Everything Policy documents is MCP-tool-shaped. (Negative finding — Cedar cannot today express "user X may call model Y" on an inference target, per docs.)
- **Dynamic/external data:** Cedar evaluation is strictly static over principal tags + action + resource + `context.input`. The only documented dynamic pattern is **interceptor-injected context**: "Cedar also enforces data-residency rules based on **context attributes injected by an interceptor**" (AWS ML blog "Secure AI agents with Policy and Lambda interceptors...", retrieved 2026-07-14) and the Well-Architected agentic lens (AGENTREL07-BP02) describes a DynamoDB/ElastiCache circuit-breaker whose state is fed to Policy "as a policy input". No native quota/counter store exists in Policy. (Negative finding for direct quota consultation.)
- **Denied call error:** policy-use-errors.md: "**AuthorizationError — The policy engine denied the request. HTTP Status Code: 403**". Body shape is not documented per-protocol (see UNVERIFIED).
- **"Token limit policy" for inference targets — documented but unmaterialized:** the inference connector page's "Response stream limits" Important box says "AgentCore Gateway does not enforce a service-level maximum on response stream duration or response size. If you do not configure a **token limit policy** on your gateway target, each request can generate an unbounded streaming response... To mitigate these risks, configure a token limit policy on your gateway targets. For more information, see **Gateway policies**" — but that link (`gateway-policies.html`) redirects to the what-is page (dead), and the June-2026 release note claims "Inference provider targets give you explicit control over... **per-model token limits**" while the provider-target docs page and the **current API model contain no token-limit field anywhere** (botocore 1.43.48 `ModelEntry = {model}` only; grep of every shape for tokenlimit/ratelimit/quota → only `ServiceQuotaExceededException`). This is exactly the docs-drift the task warned about. Also note the same box's cost warnings (shared-credential cost amplification, noisy neighbor within RPM) — useful ammunition for the metering design doc.
- Limits (policy-limitations-section.html): 10 KB/policy, 200 KB/resource, 400 KB combined Cedar schema per engine, 1,000 policies/engine, 1,000 engines/account, decimals 4 places, cannot mix `context.input.` and `context.output.` in one invocation.
- Pricing note (aws.amazon.com/bedrock/agentcore/pricing/): $0.000025 per authorization request.

### Q5. Identity available downstream

- **To the REQUEST interceptor:** the original `Authorization` header (the user's Cognito access token) is passed **iff `passRequestHeaders: true`** (Types + Configuration pages; our gateway has it true, verified live). No parsed-claims field; no identity in Lambda client context (only GATEWAY_ARN/GATEWAY_ACCOUNT_ID/REQUEST_ID/SOURCE_IP).
- **To the inference target:** outbound auth **replaces** credentials — `GATEWAY_IAM_ROLE` (SigV4; what our bedrock-mantle target uses, verified live) or `API_KEY` ("The gateway injects the stored API key into outbound requests", inference connector/provider pages). **No documented forwarding of the caller's Authorization header to inference targets and no documented X-Amzn-* identity-header injection on inference passthrough.** (Negative finding. Contrast: JWT *passthrough* patterns exist for Runtime/HTTP targets, and an interceptor could explicitly copy identity into a custom header via `transformedGatewayRequest.headers` — but bedrock-mantle wouldn't use it.)
- **Inbound validation** (gateway-inbound-auth.md): `customJWTAuthorizer` validates via `discoveryUrl` (OIDC discovery/JWKS) with `allowedClients` (against `client_id` claim), `allowedAudience` (`aud`), `allowedScopes`, and custom-claim rules (Dec 2025 release note "Custom claims value support"). Missing/invalid token → **401** with `WWW-Authenticate` advertising `scope` + `resource_metadata` (RFC 6750/9728, `/.well-known/oauth-protected-resource`); valid-but-insufficient-scope → **403** `error="insufficient_scope"`. CloudTrail logs some JWT claims incl. **Subject** (PII warning in docs). **Clock-skew tolerance: not documented.** Token refresh: nothing gateway-specific — expiry mid-stream behavior undocumented (auth is evaluated at request time; `InboundAuthorizationFailure{UnauthorizedInboundTokenException}` is the observable).

### Q6. REQUEST interceptor + streaming requests

- Docs state the REQUEST interceptor "gets invoked **before gateway makes a call to the target**" with no streaming carve-out for the request side, and inference streaming is pure passthrough ("the gateway passes through the SSE stream from the provider without transformation"). One sentence is ambiguously worded — "HTTP targets support both REQUEST and RESPONSE interceptors in buffered mode... **Interceptors are not yet supported in streaming mode**" — which read literally could exclude REQUEST interceptors on streamed calls. **Live behavior resolves the ambiguity: our REQUEST interceptor fires on every request on this gateway today, including streamed chat/completions** (substrate, live-verified 2026-07-09/10 and config re-verified today). Read the docs sentence as describing response-path processing modes.
- **Added latency: no documented figure.** Only the anecdotal ~4.5 ms average from the AWS interceptor blog. Budget one Lambda sync invoke (plus cold starts) on the request path.

---

## CLAIMED / UNVERIFIED

- **Short-circuit with arbitrary status codes (e.g. 429) + custom headers:** the output contract has unrestricted `statusCode` and `headers` fields, and docs impose no limits — but we have only proven 200 in production. A 5-minute spike (make our interceptor return 429 + `Retry-After` for a test path) would confirm. [unverified]
- **Why our inference gateway emits no Invocations/Latency metrics** (short-circuit-doesn't-count vs. not-instrumented vs. undiscovered dims): needs a controlled non-short-circuited streamed call followed by a metrics check. [unverified]
- **Error body shape returned to an OpenAI-SDK client on a Cedar deny (403 AuthorizationError) or on interceptor failure**: not documented for the `/inference` lanes. [unverified]
- **Fail-open vs fail-closed on persistent interceptor Lambda failure**: undocumented; retries are documented, terminal behavior is not. [unverified]
- **Interceptor timeout budget** (does the gateway cap Lambda execution below the function's own timeout?): undocumented. [unverified]
- **Whether gateway APPLICATION_LOGS emit anything useful for inference-target requests** (docs only describe MCP operations): requires enabling log delivery on the gateway (write action) and sending traffic. [unverified]
- **"Token limit policy"/per-model token limits for inference targets**: referenced in docs/release notes but absent from the API model — treat as roadmap, re-check the API model before the design freezes. [unverified feature]
- **JWT clock-skew tolerance and mid-stream token-expiry behavior**: undocumented. [unverified]

## Negative findings (explicit)

1. **RESPONSE interceptors are NOT invoked for streaming responses on HTTP/inference targets** — no response-side interception point exists for our SSE traffic. (Docs-confirmed, exact quote in Q1b.)
2. **No token-usage metric exists for gateway inference targets**; `InputTokenUsage`/`OutputTokenUsage`/`TokenCount` are Memory-only. (Docs + live list-metrics.)
3. **No per-caller/per-user dimension exists on any gateway metric.** Closest identity-adjacent metric is `InboundAuthorizationSuccess/Failure` per gateway, with no principal dimension. (Docs + live.)
4. **No USAGE_LOGS log type for gateways** (Runtime-only); gateway vended logs are APPLICATION_LOGS with MCP-oriented content, no token counts, no JWT sub. (Docs + live delivery-source inventory.)
5. **No documented Cedar model for inference traffic** (no inference actions, no model-id resource/context, `context.input` is MCP-tool-typed) and **no native dynamic/external data lookup in Policy** (only interceptor-injected context attributes).
6. **No identity forwarding to inference targets** — outbound SigV4/API-key replaces the caller's Authorization; no documented X-Amzn-* identity headers on inference passthrough.
7. **No path/httpMethod mutation** by REQUEST interceptors (body+headers only); only `RESPONSE_BODY` can be payload-filtered.
8. **No documented interceptor timeout/latency budget or fail-open/closed semantics.**
9. **Our gateway's inference data plane currently emits no Invocations/Latency/Duration metrics at all** (live probe) — per-request observability today is limited to inbound-auth counts plus whatever our own interceptor Lambda logs.

## Design implications (one paragraph)

The REQUEST interceptor is the only reliable, universal control point the gateway gives us on the inference path: it sees every request (all three lanes), receives the user's JWT (passRequestHeaders=true), can rewrite the JSON body (e.g., force `stream_options.include_usage`, clamp `max_tokens`, rewrite model ids) and can short-circuit with an arbitrary status code — i.e., it can do **enforcement** (pre-flight quota check against an external store) but not **measurement** of streamed output tokens. Response-side token truth for streaming must come from somewhere else (client-reported usage chunks per `include_usage`, provider-side logs, or Bedrock invocation logging — Track A/B territory). Cedar Policy and gateway metrics/logs, as shipped today, contribute approximately nothing to per-user inference metering; the referenced "token limit policy" is not yet real. Re-check `gateway-target-inference-*` docs and the botocore model before finalizing, given June-2026-era feature velocity.

## Source list (all retrieved 2026-07-14)

Docs (devguide unless noted):
- gateway-interceptors.html (+ -types.md, -configuration.md, -permissions.md, -examples.md)
- gateway-mcp-streaming.md; gateway-sessions.html (session timeouts)
- gateway-target-inference-connector.html; gateway-target-inference-provider.html
- observability-gateway-metrics.md; observability-policy-metrics.html; observability-configure.md
- policy.md; policy-authorization-flow.md; policy-core-concepts.html; policy-schema-constraints.md; policy-use-errors.md; policy-limitations-section.html; example-policies.md
- gateway-inbound-auth.md; release-notes.md (June 2026 gateway entries); bedrock-agentcore-limits.html
- API ref: API_GatewayInterceptorConfiguration, API_GatewayPolicyEngineConfiguration; CFN aws-properties-bedrockagentcore-gateway-gatewayinterceptorconfiguration / -gatewaypolicyengineconfiguration; CDK aws_bedrockagentcore (LambdaInterceptor, PolicyEngine)
- Blogs: aws.amazon.com/blogs/machine-learning/secure-ai-agents-with-policy-and-lambda-interceptors-in-amazon-bedrock-agentcore-gateway/; .../apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/; pricing: aws.amazon.com/bedrock/agentcore/pricing/

Live probes (read-only, profile prod, us-east-1, 2026-07-14): `bedrock-agentcore-control list-gateways / get-gateway / list-gateway-targets / get-gateway-target`; `cloudwatch list-metrics --namespace AWS/Bedrock-AgentCore` (41 metric names, full dim-set dump); `cloudwatch get-metric-statistics` (Invocations/InboundAuthorizationSuccess/ActiveStreamingConnections probes); `logs describe-delivery-sources`; `logs describe-log-groups --log-group-name-prefix /aws/vendedlogs/bedrock-agentcore`; local botocore 1.43.48 service-model dump for bedrock-agentcore-control.
