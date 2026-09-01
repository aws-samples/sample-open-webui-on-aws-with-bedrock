# Gateway integration and architecture

[Documentation home](README.md) · [Deployment guide](AWS_DEPLOYMENT_GUIDE.md) ·
[Metering guide](METERING.md)

This guide owns the current architecture, identity boundary, three model lanes,
and model-capability lifecycle for the sample. Open WebUI remains an unmodified
third-party image; the repository adds the Bedrock integration through AWS
infrastructure and runtime-seeded configuration.

## What the gateway solves

Open WebUI can use OpenAI-compatible connections, but a useful Bedrock model
menu needs two additional controls:

1. **A caller boundary.** Native connections and the Claude pipe send the
   signed-in user's Cognito access token to an AgentCore inference gateway. The
   gateway validates the JWT before the request reaches its target.
2. **API-aware model discovery.** Models exposed through the Mantle catalog do
   not all accept the same request API. The sample separates Chat Completions,
   Responses, and Anthropic Messages so a native connection does not advertise
   a model from the wrong API family.

The gateway is not a per-user AWS credential broker. It authenticates and
identifies the Cognito caller, then invokes Mantle with a shared gateway IAM
role.

## Canonical architecture

![Architecture flow from a user and Cognito through CloudFront, private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and optional metering services.](images/architecture-light.svg#gh-light-mode-only)
![Architecture flow from a user and Cognito through CloudFront, private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and optional metering services.](images/architecture-dark.svg#gh-dark-mode-only)

The maintainable Mermaid source and regeneration commands live in
[`diagrams/`](diagrams/README.md). This is the repository's single canonical
system topology.

### Request and trust boundaries

1. The browser reaches CloudFront over HTTPS. CloudFront uses a VPC origin to
   reach the internal ALB over HTTP; the ALB forwards HTTP to the Fargate task.
2. Cognito Managed Login performs the authorization-code flow. Open WebUI's
   built-in OIDC integration stores the user's OAuth session and synchronizes
   supported Cognito group claims.
3. At container start, the repository's seeder writes two native OpenAI
   connections and one global Claude manifold pipe into the application
   database. The official Open WebUI startup script still runs unchanged.
4. A native connection or the Claude pipe sends the user's access token to the
   AgentCore inference endpoint. The gateway's `CUSTOM_JWT` authorizer trusts
   the deployment's Cognito discovery metadata and allowed app client.
5. The gateway execution role signs the outbound Mantle request. This role—not
   a Cognito user IAM principal—is the AWS caller at that boundary.
6. The Claude pipe performs one material exception: its model-discovery hook has
   no user context, so it signs a read-only direct Mantle catalog request with
   the Fargate task role. Claude inference still uses the user's token through
   the gateway unless an operator explicitly enables the shared-role fallback.
7. Aurora PostgreSQL/pgvector, Redis, S3 uploads, and application secrets serve
   the Fargate task from the private application/data tier. NAT and managed
   service calls mean the entire data path is not VPC-only.

## Identity matrix

| Interaction | Identity presented | AWS identity used downstream | Notes |
|---|---|---|---|
| Browser sign-in | Cognito authorization-code session | Open WebUI application client | Cognito groups map to Open WebUI roles/groups. |
| Native model inference | User's Cognito access token at AgentCore | Gateway execution role to Mantle | `system_oauth`; no static provider key. |
| Claude inference | User's Cognito access token at AgentCore | Gateway execution role to Mantle | Pipe translates OpenAI-shaped input/output to Anthropic Messages. |
| Claude model discovery | No user context | Fargate task role, signed directly to Mantle | Read/list only; bypasses the gateway interceptor. |
| Claude `SIGV4_FALLBACK=true` | No user token required | Fargate task role, direct to Mantle | Off by default; loses per-user gateway attribution. |

AgentCore Policy and Bedrock Guardrails are **not configured by this
repository**. The JWT boundary is a place where an operator can design
additional controls; it is not evidence that those controls already exist.

## The three model lanes

| Lane | Seeded integration | Request path | Discovery source |
|---|---|---|---|
| **Chat Completions** | Native connection prefix `gw`, `auth_type: system_oauth` | `/inference/v1/chat/completions` | Interceptor returns the `chat_completions` list selected by `x-models-flavor`. |
| **Responses** | Native connection prefix `gwr`, `api_type: responses`, `system_oauth` | `/inference/v1/responses` | Interceptor returns the `responses` list selected by `x-models-flavor`. |
| **Anthropic Messages** | Global `gateway_anthropic` manifold pipe | `/inference/v1/messages` | Pipe signs a direct `/v1/models` catalog read, keeps available `anthropic.*` IDs, and applies its optional exact-ID allowlist. |

The checked-in `messages` array remains part of the capability snapshot used by
probe/coverage tooling, but the current Claude pipe does **not** use the gateway
model-list interceptor for discovery.

### Runtime seeding

[`pipe/seed.py`](../pipe/seed.py) waits for the required Open WebUI database
tables, then:

- upserts the Claude pipe as active and global;
- inserts or reasserts the two native gateway connections; and
- uses a `system` owner when no admin row exists yet.

Seeding is tied to schema readiness, not to a first-user or first-admin sign-in
event. It runs in the background and does not replace or patch upstream code.

## Source ownership

| Source | Responsibility |
|---|---|
| [`infra/lib/gateway-stack.ts`](../infra/lib/gateway-stack.ts) | AgentCore gateway, JWT authorizer, interceptor selection, IAM role, target custom resource, optional model refresher |
| [`gateway/interceptor/index.py`](../gateway/interceptor/index.py) | Base native-lane model-list short circuit and request passthrough |
| [`gateway/metering-interceptor/index.py`](../gateway/metering-interceptor/index.py) | Model listing plus optional quota admission, reservation, request mutation, and attribution headers |
| [`gateway/provisioner/index.py`](../gateway/provisioner/index.py) | Inference-target create/update/delete lifecycle |
| [`config/model-capabilities.json`](../config/model-capabilities.json) | Checked-in, dated lane snapshot for a specific probe context |
| [`gateway/refresher/`](../gateway/refresher/) | Optional scheduled catalog probe, collapse guard, interceptor update, target refresh, and SNS diff |
| [`pipe/seed.py`](../pipe/seed.py) | Idempotent installation of runtime connections and pipe |
| [`pipe/gateway_anthropic_pipe.py`](../pipe/gateway_anthropic_pipe.py) | Claude discovery, OAuth lookup, Messages translation, invocation, streaming, tools, images, and usage normalization |

## Native model listing

For the two native connections, Open WebUI requests `.../v1/models` and includes
an `x-models-flavor` header. The gateway invokes its global REQUEST interceptor.
The interceptor matches the path, chooses the corresponding array, prefixes
each ID with the target name (`bedrock/`), and short-circuits with an OpenAI
model-list response. Other requests pass through in the base configuration.

Path matching is intentional because the gateway event representation does not
provide a dependable distinction for the original model-list method. Filtering
at REQUEST also avoids buffering or rewriting streamed model responses.

The snapshot narrows obvious API mismatches; it is not a permanent guarantee
that every listed model will succeed. Account authorization, regional catalog
changes, provider incidents, stale target routing, and probe heuristics can
still affect a request.

## Claude translation path

The manifold pipe:

- obtains the user's access token from Open WebUI's OAuth session manager;
- translates system prompts, text/images, tool definitions, tool calls/results,
  stop sequences, and supported generation parameters to Anthropic Messages;
- routes both streaming and non-streaming responses back into OpenAI-shaped
  objects Open WebUI understands; and
- emits normalized token usage when the provider supplies it.

By default, missing OAuth state returns an instruction to sign in with SSO.
Enabling `SIGV4_FALLBACK` changes the trust and attribution model, so treat it
as an explicit architecture decision rather than a login convenience.

## Operating the model catalog

### Manual snapshot refresh

Use the deployment account/region credentials and write the reviewed result to
the checked-in file:

```bash
aws sts get-caller-identity --profile YOUR_PROFILE
python3 scripts/probe-model-capabilities.py \
  --profile YOUR_PROFILE \
  --region us-east-1 \
  --out config/model-capabilities.json \
  --yes

git diff -- config/model-capabilities.json
./deploy.sh --profile YOUR_PROFILE --region us-east-1
```

The probe tries the supported candidate API paths for every available Mantle
catalog model. A successful response—or a timeout after request acceptance—is
treated as lane support; an observed account gate excludes the model. That
heuristic is useful evidence, not a service-level guarantee.

### Scheduled refresh

Set these in `.env` before a full deployment:

```bash
ENABLE_MODEL_REFRESH=true
MODEL_REFRESH_RATE_HOURS=24
```

Then run `./deploy.sh` with the deployment's usual flags. The opt-in refresher:

1. probes the live Mantle catalog;
2. refuses a suspicious collapse of a previously populated lane;
3. updates the interceptor's served capability configuration;
4. updates the target so connector routing can refresh; and
5. publishes a lane diff to the `ModelRefreshTopicArn` SNS topic.

It is off by default. A later CDK deployment starts from the checked-in snapshot,
so commit intentional probe results even when live refresh is enabled.

## Optional metering interceptor

`./deploy.sh --metering` replaces the base models-only handler with the metering
interceptor behind a versioned `live` alias and CodeDeploy canary. Model-list
behavior remains, while inference paths add per-user admission checks,
reservations, output clamps, and project/workspace headers. The rest of that
contract—including availability-first exceptions—is owned by
[`METERING.md`](METERING.md).

## Constraints and non-claims

- The gateway validates user identity, but the gateway IAM role invokes Mantle.
- Native model lists are based on a snapshot or refresher output, not a live
  per-request capability test.
- Claude discovery is separate from native-lane listing and can surface a
  discovery-error pseudo-model if its catalog request fails.
- The checked-in snapshot is region/account specific; GatewayStack does not
  reject a different deployment region automatically.
- `SIGV4_FALLBACK` trades away per-user attribution and bypasses the gateway.
- AgentCore Policy, Guardrails, and custom per-model authorization are not part
  of the deployed default.
- `bedrock-mantle:*` remains broad on the gateway execution role because the
  connector's service authorization surface does not offer narrower resources
  in this implementation.
- Availability of AgentCore, Mantle APIs, and individual models changes. Verify
  the target account and region rather than relying on model counts in prose.

## Troubleshooting

### Native lane is empty

1. Confirm the seeder completed in `/ecs/open-webui` logs.
2. Confirm `gw`/`gwr` connection rows exist in Open WebUI.
3. Check that the expected IDs are in `config/model-capabilities.json` for the
   target region/account.
4. Inspect interceptor logs for the model-list path and flavor header.

### Claude lane is empty or shows a discovery error

The pipe reads Mantle directly with the Fargate task role for discovery. Check
its task-role permissions, region, egress, and ECS logs. An empty result can be
a valid regional catalog result.

### Model lists but invocation fails

A listed model can still be unavailable to the account or stale in connector
routing. Run the probe in the deployment context, inspect the specific Mantle
response, and either redeploy/update the target or remove the model from the
snapshot until the discrepancy is understood.

### User is told to sign in with SSO

The Claude pipe could not find a usable OAuth session and shared-role fallback
is off. Sign in through Cognito. Enable fallback only if shared-role inference
is acceptable for the deployment.

## Related guidance

- [Deployment and troubleshooting](AWS_DEPLOYMENT_GUIDE.md)
- [Consumption governance](METERING.md)
- [Open WebUI upgrade runbook](UPGRADE_RUNBOOK.md)
- [Cost planning](COSTS.md)
