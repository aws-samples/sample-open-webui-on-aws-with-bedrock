# Open WebUI on AWS with Amazon Bedrock

Deploy [Open WebUI](https://github.com/open-webui/open-webui) on AWS (Amazon ECS
on Fargate) and connect it to Amazon Bedrock models through an **Amazon Bedrock
AgentCore inference gateway** — an AWS deployment sample.

> **About the application.** This is a deployment sample for the third-party
> Open WebUI project by Open WebUI Inc. Open WebUI is **not included in this
> repository** and is licensed separately under the Open WebUI License (see
> [`NOTICE`](NOTICE) and [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md)).
>
> The deployed container is the **completely unmodified official Open WebUI
> image**, pinned by digest (currently **v0.10.2**) and pulled from
> `ghcr.io/open-webui/open-webui` at deploy time. There is **no fork, no
> patches, and no image build**. The Amazon Bedrock integration is delivered
> entirely as AWS infrastructure + runtime configuration:
>
> 1. an **AgentCore inference gateway** that fronts Amazon Bedrock's
>    OpenAI-compatible endpoint, authenticated per-user via Amazon Cognito; and
> 2. a small Open WebUI **pipe function** for Anthropic Claude models (which are
>    Messages-API-only on Bedrock), plus two native OpenAI connections — all
>    seeded into the app database at container start.
>
> Everything AWS-authored lives under [`infra/`](infra/) (CDK), [`gateway/`](gateway/)
> (interceptor + provisioner Lambdas), [`pipe/`](pipe/) (the Claude pipe + seeder),
> [`config/`](config/), [`scripts/`](scripts/), and [`docs/`](docs/).

The upstream pin is **v0.10.2** because that release contains upstream security
and access-control fixes; [`docs/UPGRADE_RUNBOOK.md`](docs/UPGRADE_RUNBOOK.md)
covers moving the pin.

## Why a gateway?

Amazon Bedrock exposes an OpenAI-compatible endpoint (`bedrock-mantle`) that
Open WebUI can talk to natively — but with two wrinkles this sample solves:

- **Per-user identity & governance.** The AgentCore gateway accepts the
  logged-in user's own Cognito OAuth token (Open WebUI's `system_oauth`
  connection auth). Every model call reaches Bedrock **as that user**, ready for
  [Amazon Bedrock AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
  (Cedar), Guardrails, and per-user throttling — with no static API keys.
- **Only-working-models.** Bedrock models don't all support the same API
  (Anthropic Claude is Messages-only; the GPT-5.x family is Responses-only;
  most others are Chat Completions). A gateway interceptor filters the model
  listing per connection so Open WebUI **only ever surfaces models that
  actually work** — nothing that would error when a user picks it.

The result: one governed endpoint, three lanes, every compatible Bedrock model
functional in the Open WebUI dropdown with per-user identity end to end.

## Architecture

```
                            End users
                                │ HTTPS
                        ┌───────▼────────┐
                        │   CloudFront    │
                        └───────┬────────┘
                                │ VPC origin
┌───────────────────────────────┼──────────────────────────────────────────┐
│ VPC (private subnets)          │                                          │
│                        ┌───────▼────────┐                                 │
│                        │  Internal ALB   │                                 │
│                        └───────┬────────┘                                 │
│                    ┌───────────▼───────────┐    ┌────────────────────┐    │
│                    │  ECS Fargate            │    │  Aurora PostgreSQL  │    │
│                    │  UNMODIFIED official     │◄──►│  (pgvector)         │    │
│                    │  Open WebUI (v0.10.2)    │    └────────────────────┘    │
│                    │  + Claude pipe +        │    ┌────────────────────┐    │
│                    │    2 OpenAI connections │◄──►│  ElastiCache Redis  │    │
│                    └───────┬─────────────────┘    └────────────────────┘    │
└────────────────────────────┼──────────────────────────────────────────────┘
   user's OAuth token (system_oauth) │ per-user JWT
                    ┌────────────────▼───────────────┐
                    │  AgentCore inference gateway     │  CUSTOM_JWT (Cognito)
                    │  • REQUEST interceptor:          │  + models-filter Lambda
                    │    capability-filtered listing   │
                    │  • bedrock-mantle target         │  GATEWAY_IAM_ROLE (SigV4)
                    └────────────────┬─────────────────┘
                    ┌────────────────▼───────────────┐
                    │  Amazon Bedrock (bedrock-mantle) │
                    └──────────────────────────────────┘
```

The user's identity flows Cognito → gateway → Bedrock the whole way. See
[`docs/GATEWAY_INTEGRATION_GUIDE.md`](docs/GATEWAY_INTEGRATION_GUIDE.md) for the
full design.

## The three model lanes

All three are seeded automatically at container start
([`pipe/seed.py`](pipe/seed.py)); all authenticate with the user's own OAuth
token through the one gateway.

| Lane | How it's wired | Models it serves |
|---|---|---|
| **Chat Completions** | native OpenAI connection (`system_oauth`), interceptor flavor `chat_completions` | the majority — Qwen, DeepSeek, Mistral, gpt-oss, Gemma, etc. |
| **Responses** | native OpenAI connection (`system_oauth`, `api_type: responses`) | the Responses-only family (e.g. GPT-5.x) + gpt-oss |
| **Messages (Claude)** | the [`pipe/gateway_anthropic_pipe.py`](pipe/gateway_anthropic_pipe.py) manifold pipe | Anthropic Claude (Messages-API-only on Bedrock) |

Which model ids fall in each lane is data, not code:
[`config/model-capabilities.json`](config/model-capabilities.json), regenerated
with [`scripts/probe-model-capabilities.py`](scripts/probe-model-capabilities.py).

## Prerequisites

- An AWS account with **Amazon Bedrock model access** enabled for the models you
  want ([Bedrock console → Model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)).
- **AWS CLI v2**, **Node.js 20+**, and **npm**. (No Docker — there is no image
  build.)
- CDK bootstrapped in your target account/region (`npx cdk bootstrap`), or let
  `deploy.sh` do it.
- A region where both Amazon Bedrock (`bedrock-mantle`) and CloudFront VPC
  origins are available. **Model availability on `bedrock-mantle` is
  region-dependent** — notably Anthropic Claude (the Messages lane) is offered
  in **`us-east-1`** (and partially `us-west-2`) but **not `us-east-2`** as of
  2026-07. Deploy to **`us-east-1`** for the full three-lane experience; other
  regions serve whatever their `bedrock-mantle` catalog includes. Regenerate
  [`config/model-capabilities.json`](config/model-capabilities.json) for your
  region with [`scripts/probe-model-capabilities.py`](scripts/probe-model-capabilities.py).

## Quick start

```bash
git clone https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock.git
cd sample-open-webui-on-aws-with-bedrock

cp .env.example .env      # review; no Bedrock vars needed (gateway handles it)
./deploy.sh               # interactive: pick profile + region, then deploy
```

`deploy.sh` deploys five CDK stacks (Network → Data → Auth → Gateway → Compute),
then prints the CloudFront URL. First deploy takes ~25–35 min (Aurora + Redis +
CloudFront are the long poles). Then:

1. Open the CloudFront URL and sign in with **Amazon Cognito** (create a user in
   the Cognito console first, or enable self-signup — see the deployment guide).
   The **first** user to sign in becomes the admin.
2. The Bedrock models appear in the model dropdown within a minute of first
   admin sign-in (the seeder installs the pipe + connections on that event).

Full instructions — Cognito user setup, custom domains, model access control:
[`docs/AWS_DEPLOYMENT_GUIDE.md`](docs/AWS_DEPLOYMENT_GUIDE.md).

## Repository layout

```
infra/                     CDK app (TypeScript)
  bin/app.ts               5 stacks: Network, Data, Auth, Gateway, Compute
  lib/gateway-stack.ts     AgentCore gateway + interceptor + inference target
  lib/compute-stack.ts     ECS Fargate running the unmodified official image
  lib/{network,data,auth}-stack.ts
gateway/
  interceptor/index.py     REQUEST interceptor: capability-filtered model listing
  provisioner/index.py     custom resource: creates the bedrock-mantle inference target
pipe/
  gateway_anthropic_pipe.py  Claude manifold pipe (per-user OAuth to the gateway)
  seed.py                    installs the pipe + 2 OpenAI connections at boot
config/model-capabilities.json   which models work on which API (interceptor input)
scripts/probe-model-capabilities.py   regenerate the capability matrix
deploy.sh                  one-command deploy
docs/                      deployment, gateway integration, upgrade, cost
```

## Cost

Infrastructure (VPC/ALB/ECS/Aurora/Redis/CloudFront/gateway/Lambdas) is a small
fixed monthly cost; the dominant driver is Bedrock token consumption, which is
pay-per-use. See [`docs/COST_ANALYSIS_20K_USERS.md`](docs/COST_ANALYSIS_20K_USERS.md).

**Optional metering module** (`./deploy.sh --metering`, off by default): per-user
token/dollar metering, per-team cost attribution via Bedrock Projects, and
operator-set quotas enforced at the gateway (blocked users see the reason in
the chat). When disabled, the base sample is byte-identical. See
[`docs/METERING.md`](docs/METERING.md).

## Security

Private ALB (CloudFront-only ingress via VPC origin), all compute/data in
private subnets, TLS in transit, encryption at rest, secrets in AWS Secrets
Manager, Cognito SSO, and per-user identity on every model call through the
gateway. See the Security section of the deployment guide.

## License

This sample is licensed under **MIT-0** (see [`LICENSE`](LICENSE)). The Open
WebUI application it deploys is a separate third-party project under its own
license — see [`NOTICE`](NOTICE) and [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md).
