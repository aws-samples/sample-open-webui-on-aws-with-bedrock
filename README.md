# Open WebUI on AWS with Amazon Bedrock

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Infrastructure: AWS CDK v2](https://img.shields.io/badge/Infrastructure-AWS%20CDK%20v2-orange.svg)](infra/)

Deploy the **unmodified official Open WebUI image** on Amazon ECS with AWS
Fargate, then connect signed-in users to Amazon Bedrock through an **Amazon
Bedrock AgentCore inference gateway**. The repository does not include, fork,
patch, or build Open WebUI; it supplies AWS infrastructure and runtime
configuration around that separately licensed application.

## Why this sample is different

| Capability | What the sample demonstrates |
|---|---|
| **A user-aware inference boundary** | Open WebUI sends the signed-in user's Cognito access token to AgentCore. The gateway validates that JWT, then invokes the Bedrock-compatible endpoint with its own IAM role—no static model API key in the application. |
| **One model menu, three API lanes** | Two native OpenAI connections serve Chat Completions and Responses. A manifold pipe translates Claude requests to Anthropic Messages. A probed capability snapshot filters the native model lists so API-incompatible choices are not offered by those connections. |
| **Optional consumption governance** | `./deploy.sh --metering` adds pre-request per-user USD/RPM checks, usage settlement into a DynamoDB ledger, Bedrock Project headers for team attribution, live pricing coverage, alarms, and a Cloudscape admin/self-service console. The module is off by default. |

> [!CAUTION]
> **This is sample code for demonstration and evaluation—not a production
> deployment.** It has not been through an application security review. Before
> using it outside a test environment, perform your own security review and
> threat model, harden it for your requirements, test failure modes and scale,
> and review the [production considerations](docs/AWS_DEPLOYMENT_GUIDE.md#production-considerations)
> and [`DISCLAIMER.txt`](DISCLAIMER.txt).

![Architecture flow from a user and Cognito through CloudFront, private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and optional metering services.](docs/images/architecture-light.svg#gh-light-mode-only)
![Architecture flow from a user and Cognito through CloudFront, private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and optional metering services.](docs/images/architecture-dark.svg#gh-dark-mode-only)

**Jump to:** [Quick start](#quick-start) · [Gateway design](#the-gateway-path) ·
[Consumption governance](#optional-consumption-governance) ·
[Documentation](#documentation) · [Costs](docs/COSTS.md)

## What gets deployed

The base deployment creates five CDK stacks:

- **Network** — a VPC, public/private subnets, NAT gateways, security groups, and
  selected VPC endpoints.
- **Data** — Aurora PostgreSQL Serverless v2 with pgvector, ElastiCache Redis,
  and an S3 upload bucket.
- **Auth** — a Cognito user pool, Managed Login domain, OAuth app client, and
  role-mapping groups.
- **Gateway** — the AgentCore gateway, Cognito JWT authorizer, REQUEST
  interceptor, and `bedrock-mantle` inference target.
- **Compute** — CloudFront, a VPC origin, an internal Application Load Balancer,
  and Fargate tasks running the official Open WebUI image.

`./deploy.sh --metering` adds a sixth stack and metering integrations in the
Gateway and Compute stacks. The base deployment remains the default.

## Quick start

### Prerequisites

- An AWS account and credentials authorized to create the resources above.
- AWS CLI v2, Node.js 20 or newer, npm, and Python 3 with pip.
- A target region that supports the required AgentCore, Bedrock, CloudFront VPC
  origin, database, and container services. `us-east-1` is the documented
  default for the full three-lane experience; service and model availability
  changes, so verify it for your account and region.
- Access to the Bedrock models you intend to test.

Docker is not required because this repository builds no application image.

### Deploy

```bash
git clone https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock.git
cd sample-open-webui-on-aws-with-bedrock

cp .env.example .env
./deploy.sh
```

The script prompts for an AWS profile and region, bootstraps CDK when needed,
resolves the selected official Open WebUI release to an image digest, deploys
the stacks, reconciles Cognito callback URLs and secrets, and prints the
application URL. For a non-interactive deployment:

```bash
./deploy.sh --profile YOUR_PROFILE --region us-east-1 --yes
```

After deployment, register a test user in the Cognito user pool and add the
initial operator to the `admin` group before signing in. The startup seeder
waits for the Open WebUI schema, then installs the two native gateway
connections and the Claude manifold pipe; it does not depend on a first-admin
login event.

For prerequisites, configuration, validation, troubleshooting, and cleanup,
follow the [deployment guide](docs/AWS_DEPLOYMENT_GUIDE.md).

## The gateway path

The identity boundary is deliberate:

1. Cognito authenticates the user and Open WebUI retains the OAuth session.
2. Each model request carries that user's access token to AgentCore.
3. AgentCore validates the token against the Cognito user pool and app client.
4. The gateway execution role signs the outbound request to `bedrock-mantle`.

The user is identifiable **at the gateway**; Amazon Bedrock is not invoked as a
per-user IAM principal. The three runtime-seeded lanes are:

| Lane | Open WebUI integration | Model discovery |
|---|---|---|
| Chat Completions | Native OpenAI connection, `system_oauth` | REQUEST interceptor returns the `chat_completions` list from the probed snapshot. |
| Responses | Native OpenAI Responses connection, `system_oauth` | REQUEST interceptor returns the `responses` list from the probed snapshot. |
| Anthropic Messages | Claude manifold pipe with OpenAI↔Messages translation | Pipe performs a read-only, task-role-signed Mantle catalog lookup and keeps available `anthropic.*` models. |

The checked-in [`config/model-capabilities.json`](config/model-capabilities.json)
is a dated region/account snapshot, not a permanent availability promise.
Regenerate it for the deployment context or enable the opt-in scheduled
refresher. See the [gateway integration guide](docs/GATEWAY_INTEGRATION_GUIDE.md)
for request paths, identity semantics, source ownership, and limitations.

## Optional consumption governance

Enable the module with:

```bash
./deploy.sh --metering
```

The module makes consumption visible and actionable without modifying Open
WebUI:

- the gateway interceptor reads a per-user monthly USD counter/policy and RPM
  bucket before inference, records a conservative estimate, and emits an
  OpenAI-shaped 429 when an already-recorded limit is exceeded in ENFORCE mode;
- a seeded global filter captures persisted provider usage and sends it through
  EventBridge to an idempotent settlement path;
- DynamoDB stores reservations, the append-only usage ledger, counters,
  policies, audit records, pricing, and the gateway↔pricing coverage join;
- Bedrock Project/workspace headers provide a best-effort team-attribution path
  when project tags are activated for billing;
- a separate Cognito/PKCE Cloudscape console gives all users their own usage
  view and gives admin-group members governance controls; and
- pricing, canary, recovery, reconciliation, dashboard, alarm, and SNS paths
  make important gaps observable.

This is an **availability-first** design, not an exact billing boundary. It
enforces USD and request-rate policies—not monthly token quotas or group
quotas. Concurrent requests can cross a limit before the next-request block;
unpriced requests remain available and record $0 until a rate is resolved;
and the default sweeper refunds reservations that never settle. Read the
[governance contract and operator guide](docs/METERING.md) before enabling it.

## Important constraints

- **Third-party application:** Open WebUI is developed and licensed separately.
  AWS does not maintain or support it. Review [`NOTICE`](NOTICE) and
  [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md).
- **No image build:** the supported deploy path selects an official Open WebUI
  release and normally resolves it to an immutable digest. Registry-resolution
  failure and custom image overrides have different guarantees; see the
  [upgrade runbook](docs/UPGRADE_RUNBOOK.md).
- **Region-specific models:** the native lane lists must match the deployment
  account and region. The scheduled capability refresher is off by default.
- **Network boundary:** viewers use HTTPS to CloudFront. CloudFront uses an HTTP
  VPC-origin hop to the internal ALB, and the ALB uses HTTP to the task. The
  application and data tier are private, but managed-service and registry
  traffic is not represented as VPC-only.
- **Retained data:** some stateful resources are retained during stack removal
  and can continue billing until explicitly handled. Read cleanup instructions
  before deploying.
- **Variable cost:** model usage, NAT processing, database capacity, logs,
  transfer, and optional metering resources vary by region and workload. Use
  the [cost-planning guide](docs/COSTS.md), not a static monthly estimate.

## Documentation

Start at the [documentation home](docs/README.md), or choose a path:

| Goal | Read |
|---|---|
| Decide whether the architecture fits | [Gateway integration guide](docs/GATEWAY_INTEGRATION_GUIDE.md) and [cost planning](docs/COSTS.md) |
| Deploy, validate, operate, or remove it | [AWS deployment guide](docs/AWS_DEPLOYMENT_GUIDE.md) |
| Evaluate or operate consumption governance | [Metering and quota guide](docs/METERING.md) |
| Change or roll back the upstream image | [Upgrade runbook](docs/UPGRADE_RUNBOOK.md) |
| Work on the implementation | [`infra/README.md`](infra/README.md), [`pipe/README.md`](pipe/README.md), and [`console/README.md`](console/README.md) |
| Understand past decisions | [Historical plans](docs/plans/README.md), [reviews](docs/reviews/README.md), and [captured learnings](docs/solutions/README.md) |

## Contributing, security, and license

See [`CONTRIBUTING.md`](CONTRIBUTING.md) before proposing changes and
[`SECURITY.md`](SECURITY.md) for vulnerability reporting.

This sample is licensed under [MIT-0](LICENSE). Open WebUI and the dependency
trees retain their own licenses and notices; see
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md).
