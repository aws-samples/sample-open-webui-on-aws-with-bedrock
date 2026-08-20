# Open WebUI on AWS — Implementation Guide

This implementation guide provides an overview of deploying Open WebUI on Amazon Web Services (AWS) with Amazon Bedrock integration delivered through an **Amazon Bedrock AgentCore inference gateway**. It covers the reference architecture and components, considerations for planning the deployment, security best practices, and step-by-step configuration instructions. This guide is intended for Solutions Architects, DevOps engineers, platform engineers, and IT administrators who want to deploy a self-hosted, multi-provider generative AI chat platform on AWS.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Plan Your Deployment](#plan-your-deployment)
- [Security](#security)
- [Production Considerations](#production-considerations)
- [Deployment](#deployment)
- [Post-Deployment Configuration](#post-deployment-configuration)
- [Operations and Monitoring](#operations-and-monitoring)
- [Updating the Solution](#updating-the-solution)
- [Uninstall the Solution](#uninstall-the-solution)
- [Troubleshooting](#troubleshooting)

---

## Overview

This implementation guide provides an automated AWS Cloud Development Kit (AWS CDK) deployment of [Open WebUI](https://docs.openwebui.com/) onto Amazon Elastic Container Service (Amazon ECS) with Fargate. It is pre-configured with defaults that let most users quickly stand up a self-hosted AI chat platform with Amazon Bedrock models available in the model dropdown, with production-oriented security features suitable for evaluation and customization before production use.

> **This is sample code, not a production deployment.** It is provided for demonstration and evaluation purposes and has not been through an application security review. Before deploying it in a production environment, perform your own security review, threat modeling, and testing against your organization's requirements, and complete the hardening steps in [Security](#security) and [Production Considerations](#production-considerations). See [`DISCLAIMER.txt`](../DISCLAIMER.txt).

The deployed container is the **completely unmodified official Open WebUI image**, pulled from `ghcr.io/open-webui/open-webui` at deploy time. **There is no fork and no image build; the upstream code is not modified in any way** — Docker is not a prerequisite. By default a deploy runs the **latest official Open WebUI release**, resolved to an immutable image digest at deploy time; the `OPEN_WEBUI_IMAGE` variable in `.env` pins a specific release tag or digest instead. The Amazon Bedrock integration is delivered entirely as AWS infrastructure plus runtime configuration:

1. an **AgentCore inference gateway** (its own CDK stack) that fronts Amazon Bedrock's OpenAI-compatible endpoint (`bedrock-mantle`), authenticated per user with Amazon Cognito JWTs; and
2. a small Open WebUI **Claude pipe function** plus **two native OpenAI connections**, all seeded into the app database at container start by [`pipe/seed.py`](../pipe/seed.py).

Run **v0.10.2 or newer** (the default — the latest release — always satisfies this): that release contains upstream security and access-control fixes. [`UPGRADE_RUNBOOK.md`](UPGRADE_RUNBOOK.md) covers version selection, upgrades, and rollback. For the full technical design of the gateway, interceptor, capability matrix, and pipe, see the companion [`GATEWAY_INTEGRATION_GUIDE.md`](GATEWAY_INTEGRATION_GUIDE.md).

### Features and Benefits

- **Per-user identity to Amazon Bedrock** — Every model call reaches Bedrock as the logged-in user. The AgentCore gateway's `CUSTOM_JWT` authorizer trusts the deployment's Cognito user pool and validates the user's own OAuth token (Open WebUI's `system_oauth` connection auth) on every request — ready for [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html) (Cedar), Guardrails, and per-user throttling, with no static API keys.
- **Only-working models in the dropdown** — Bedrock models don't all share the same API (Anthropic Claude is Messages-only, the GPT-5.x family is Responses-only, most others are Chat Completions). A gateway interceptor filters the model listing per connection so Open WebUI only ever surfaces models that actually work.
- **Unmodified upstream image** — The official Open WebUI release runs as-is. No build pipeline, no patched code, no drift from upstream. The deployed version is operator-selectable in one place (`OPEN_WEBUI_IMAGE` in `.env`, resolved to an immutable digest at deploy time); see the upgrade runbook.
- **Enterprise SSO authentication** — Amazon Cognito via Open WebUI's built-in OIDC support. Group-based role mapping syncs Cognito groups to Open WebUI roles and groups on every login. Zero custom auth code.
- **Group-based model access control** — Restrict which models are available to which Cognito groups using Open WebUI's native RBAC (Workspace → Models).
- **Fully private networking** — Internal ALB with CloudFront VPC origin. No public-facing load balancer. All data-plane traffic stays within the VPC.
- **Serverless data tier** — Aurora PostgreSQL Serverless v2 (auto-scaling) with pgvector, and ElastiCache Redis with TLS encryption for shared session/Socket.IO state.
- **One-command deployment** — A single `./deploy.sh` deploys all five CDK stacks and prints the CloudFront URL.

### Use Cases

- **Enterprise AI chat platform** — Provide employees with a ChatGPT-like interface backed by Amazon Bedrock models, with SSO authentication and per-user identity end to end.
- **Multi-model evaluation** — Compare responses across Claude, and the many Chat Completions / Responses models on `bedrock-mantle`, side-by-side in a single interface.
- **Governed AI access** — SSO-gated access with group-based model restrictions and per-user identity at the gateway. Pair with AWS-native cost controls (Bedrock service quotas, CloudWatch usage metrics, AWS Budgets), or enable the opt-in metering module (`./deploy.sh --metering`, [`METERING.md`](METERING.md)) for per-user quotas enforced at the gateway.

---

## Architecture

### Architecture Diagram

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
│                    │  Open WebUI image        │    └────────────────────┘    │
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

### Architecture Steps

1. End users access the application through an Amazon CloudFront distribution, which provides HTTPS termination, CDN caching for static assets, and optional custom domain support via AWS Certificate Manager (ACM).
2. CloudFront forwards requests to an internal Application Load Balancer (ALB) via a **VPC origin**. The ALB is not internet-facing — CloudFront creates ENIs in the VPC for private connectivity. WebSocket upgrades pass through the VPC origin end to end.
3. The ALB routes traffic to Amazon ECS Fargate tasks running the **unmodified official Open WebUI image**, pulled from `ghcr.io/open-webui/open-webui`. The deploy resolves the selected version to an immutable digest, so every task runs identical bytes. No image is built.
4. For Bedrock model traffic, the ECS task calls the **AgentCore inference gateway** rather than Bedrock directly. Each request carries the logged-in user's own Cognito OAuth token. The gateway validates that JWT (inbound, `CUSTOM_JWT`) and signs to the `bedrock-mantle` OpenAI-compatible endpoint with the gateway execution role (outbound, SigV4). A REQUEST interceptor Lambda returns a capability-filtered model list so only working models appear. See [`GATEWAY_INTEGRATION_GUIDE.md`](GATEWAY_INTEGRATION_GUIDE.md) for the full design.
5. Amazon Cognito provides SSO authentication. Users authenticate via the Cognito Managed Login UI, and the callback auto-provisions users with role mapping based on Cognito group membership. The same Cognito user pool is the trust anchor for the gateway's JWT authorizer.
6. Aurora PostgreSQL Serverless v2 (with pgvector) stores application data (users, chats, configurations, migrations). ElastiCache Redis provides session caching and shared Socket.IO state with TLS encryption. Amazon S3 stores uploaded files.
7. AWS Secrets Manager stores sensitive credentials (WEBUI_SECRET_KEY, Cognito client secret, database password). VPC endpoints provide private connectivity to AWS services without traversing the public internet.

### The three model lanes

All three lanes are seeded automatically at container start ([`pipe/seed.py`](../pipe/seed.py)); all authenticate with the user's own OAuth token through the one gateway.

| Lane | How it's wired | Models it serves |
|---|---|---|
| **Chat Completions** | native OpenAI connection (`system_oauth`, prefix `gw`), interceptor flavor `chat_completions` | the majority — Qwen, DeepSeek, Mistral, gpt-oss, Gemma, etc. (~38 models verified) |
| **Responses** | native OpenAI connection (`system_oauth`, prefix `gwr`, `api_type: responses`) | the Responses-only family, e.g. GPT-5.x (~6 models verified) |
| **Messages (Claude)** | the [`pipe/gateway_anthropic_pipe.py`](../pipe/gateway_anthropic_pipe.py) manifold pipe | Anthropic Claude, Messages-API-only on Bedrock (5 models verified) |

Which model ids fall in each lane is data, not code — [`config/model-capabilities.json`](../config/model-capabilities.json), regenerated with [`scripts/probe-model-capabilities.py`](../scripts/probe-model-capabilities.py).

### AWS Services in This Solution

| AWS Service | Role | Description |
|---|---|---|
| Amazon CloudFront | Core | HTTPS termination, CDN, custom domain support, DDoS protection via AWS Shield Standard |
| Amazon ECS (Fargate) | Core | Serverless container compute running the unmodified official Open WebUI image, with auto-scaling |
| Amazon Bedrock AgentCore | Core | Inference gateway fronting `bedrock-mantle` with a per-user `CUSTOM_JWT` authorizer |
| Amazon Bedrock | Core | LLM provider via the `bedrock-mantle` OpenAI-compatible endpoint |
| Amazon Cognito | Core | SSO authentication with group-based RBAC, and the trust anchor for the gateway JWT authorizer |
| Amazon VPC | Core | Isolated network with private subnets, NAT gateway, and VPC endpoints |
| Application Load Balancer | Supporting | Internal load balancing (private, accessed only via CloudFront VPC origin) |
| Aurora PostgreSQL Serverless v2 (17.7 LTS) | Supporting | Auto-scaling database with pgvector for application data |
| Amazon ElastiCache (Redis) | Supporting | Session cache and Socket.IO state sharing with TLS encryption |
| Amazon S3 | Supporting | File upload storage |
| AWS Lambda | Supporting | Gateway REQUEST interceptor + inference-target provisioner (custom resource) |
| AWS Secrets Manager | Supporting | Secure storage for credentials |
| AWS Certificate Manager (ACM) | Security | TLS certificates for custom domain (optional) |
| Amazon CloudWatch | Monitoring | Container logs, metrics, and alarms |
| AWS IAM | Security | Least-privilege roles for ECS tasks, the gateway (`bedrock-mantle` SigV4), and service roles |

### CDK Stacks

`deploy.sh` deploys five CDK stacks in dependency order (see [`infra/bin/app.ts`](../infra/bin/app.ts)):

| Stack | Purpose |
|---|---|
| `OpenWebUI-Network` | VPC, subnets, NAT, security groups, VPC endpoints |
| `OpenWebUI-Data` | Aurora PostgreSQL Serverless v2 (pgvector), ElastiCache Redis, S3 upload bucket |
| `OpenWebUI-Auth` | Cognito user pool, app client, and Managed Login domain |
| `OpenWebUI-Gateway` | AgentCore inference gateway, REQUEST interceptor Lambda, `bedrock-mantle` inference target (custom resource) |
| `OpenWebUI-Compute` | ECS Fargate service (unmodified official image), internal ALB, CloudFront distribution |

A sixth, opt-in stack — `OpenWebUI-Metering` — is added by `./deploy.sh --metering` (see [`METERING.md`](METERING.md)); everything in this guide applies unchanged with it enabled.

---

## Plan Your Deployment

### Prerequisites

Before deploying, ensure you have:

1. **AWS Account** with permissions to create IAM roles, VPCs, ECS clusters, CloudFront distributions, Cognito user pools, Aurora clusters, ElastiCache clusters, S3 buckets, Lambda functions, and AgentCore gateways.
2. **AWS CLI v2** installed and configured with credentials (SSO profiles supported).
3. **Node.js 20+** and **npm** (for the AWS CDK). **Docker is not required** — there is no image build.
4. **Amazon Bedrock model access** enabled in your target region for the models you want. Go to the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/) → Model access → enable the models you want to use.
5. **CDK bootstrapped** in your target account/region (`npx cdk bootstrap`), or let `deploy.sh` do it for you.
6. **(Optional) Custom domain** — An ACM certificate in `us-east-1` for your domain. You will create a DNS CNAME record pointing to the CloudFront distribution after deployment.

### Region requirements

Deploy in a region that supports **both** of:

- **Amazon Bedrock `bedrock-mantle`** (the OpenAI-compatible endpoint the gateway fronts). Its availability is a subset of Bedrock regions — verify before deploying.
- **CloudFront VPC origins** (used to keep the ALB private).

See [Supported AWS Regions](#supported-aws-regions) for tested regions.

### Cost

Infrastructure (VPC/ALB/ECS/Aurora/Redis/CloudFront/gateway/Lambdas) is a small fixed monthly cost; the dominant cost driver is Amazon Bedrock token consumption, which is pay-per-use and varies by usage. Aurora Serverless v2 scales down during idle periods, and ECS auto-scaling adds tasks under load. We recommend creating a [budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) to monitor spending. For a detailed cost model at scale, see [`COST_ANALYSIS_20K_USERS.md`](COST_ANALYSIS_20K_USERS.md).

### Known Limitations

- **WebSocket is required end-to-end.** CloudFront supports WebSocket over VPC origins, and the stack pins `ENABLE_WEBSOCKET_SUPPORT=true` (Socket.IO runs websocket-only, with Redis-shared state across tasks). If you front the ALB with something other than this stack's CloudFront distribution, it must pass WebSocket upgrades through.
- **DNS is managed externally.** The CDK stacks do not create Route 53 records. If using a custom domain, you must create a CNAME record pointing to the CloudFront distribution domain after deployment.
- **New Bedrock models require a probe + redeploy.** Models don't appear in the dropdown until they're in `config/model-capabilities.json`. Re-run [`scripts/probe-model-capabilities.py`](../scripts/probe-model-capabilities.py) and redeploy the Gateway stack to refresh the interceptor (see the gateway guide) — or enable the opt-in scheduled refresher (`ENABLE_MODEL_REFRESH=true`, see the [gateway guide](GATEWAY_INTEGRATION_GUIDE.md#operational-notes)) to automate it.
- **Single-region deployment.** The CDK stacks deploy to a single AWS region.

### Supported AWS Regions

This solution can be deployed in any AWS region that supports all required services (notably Amazon Bedrock `bedrock-mantle` and CloudFront VPC origins). It has been tested in:

| Region Name | Region Code | Notes |
|---|---|---|
| US East (N. Virginia) | us-east-1 | Full three-lane experience (recommended) |
| US East (Ohio) | us-east-2 | No Anthropic Claude on `bedrock-mantle` as of 2026-07 — the Messages lane is empty |
| US West (Oregon) | us-west-2 | Partial Claude availability as of 2026-07 |

Model availability on `bedrock-mantle` is region-dependent; regenerate [`config/model-capabilities.json`](../config/model-capabilities.json) for your region (see [Adding or Refreshing Models](#adding-or-refreshing-models)).

---

## Security

When you build systems on AWS infrastructure, security responsibilities are shared between you and AWS. This [shared responsibility model](https://aws.amazon.com/compliance/shared-responsibility-model/) reduces your operational burden. For more information, visit [AWS Cloud Security](https://aws.amazon.com/security/).

### Network Security

- **Private ALB** — The Application Load Balancer is internal (not internet-facing). It has no public ingress rules. Only CloudFront can reach it via VPC origin ENIs.
- **Private subnets** — All compute (ECS), database (Aurora), and cache (Redis) resources run in private subnets with no direct internet access.
- **VPC endpoints** — Private connectivity to AWS services (S3, CloudWatch, Secrets Manager, and others) without traversing the public internet.
- **NAT Gateway** — Outbound internet access (for pulling the official image at deploy time and reaching AWS APIs where a VPC endpoint is not used) routes through a NAT Gateway in a public subnet.

### Data Protection

- **Encryption in transit** — All connections use TLS: CloudFront → ALB (HTTP within the VPC), Redis (`rediss://` scheme), Aurora (SSL enforced), S3 (HTTPS), and gateway → `bedrock-mantle` (HTTPS, SigV4).
- **Encryption at rest** — Aurora PostgreSQL, ElastiCache Redis, S3, and ECS ephemeral storage use AWS-managed encryption keys.
- **Secrets Manager** — WEBUI_SECRET_KEY, the Cognito client secret, and the database password are stored in AWS Secrets Manager and injected into the ECS task definition at runtime. They are never stored in `.env` files or source code.

### Identity and Access Management

- **Per-user identity to Bedrock** — Model calls do not use a shared task-role identity. Each request carries the logged-in user's own Cognito OAuth token to the AgentCore gateway, whose `CUSTOM_JWT` authorizer validates it against the deployment's Cognito user pool and app client. Model traffic is therefore attributable per user and governable with AgentCore Policy (Cedar) and Guardrails. See [`GATEWAY_INTEGRATION_GUIDE.md`](GATEWAY_INTEGRATION_GUIDE.md).
- **Gateway execution role (outbound)** — The gateway signs requests to `bedrock-mantle` with SigV4 using its own execution role, which holds `bedrock-mantle:*` (note: `bedrock-mantle` is its own IAM service prefix; plain `bedrock:*` is not sufficient for the OpenAI-compatible endpoint).
- **ECS task role** — Follows least-privilege. S3 access scoped to the deployment bucket; Secrets Manager access scoped to deployment secrets.
- **Cognito SSO** — Users authenticate via Cognito Managed Login. Group membership determines role (admin vs. user). No local passwords are stored for SSO users.
- **Group-based model access** — Model visibility can be restricted per Cognito group using Open WebUI's native RBAC (Workspace → Models).

### Authentication Flow

Authentication uses Open WebUI's **built-in OIDC support** — Cognito is configured as a standard OIDC provider via environment variables. Zero custom auth code.

1. User clicks "Amazon Cognito" on the login page → redirected to Cognito Managed Login UI.
2. User authenticates (Cognito user pool credentials, or federated identity provider if configured).
3. Cognito redirects to `/oauth/oidc/callback` with an authorization code.
4. Open WebUI's built-in OAuth handler exchanges the code for tokens using the client secret from Secrets Manager.
5. User is auto-provisioned (or updated) based on the ID token claims. **The first user to sign in becomes the admin.**
6. Cognito groups are synced to Open WebUI groups (`ENABLE_OAUTH_GROUP_MANAGEMENT=true`).
7. Role mapping: users in `admin`/`webui-admins`/`admins` Cognito groups → admin role, all others → user role (`OAUTH_ROLES_CLAIM=cognito:groups`).
8. A session is established, and the user's OAuth token becomes available for the `system_oauth` connections that reach Bedrock through the gateway (per-user identity).

### Production Considerations

This repository is sample code. The defaults above are security-conscious, but they have not been through an application security review, and the sample is not certified for production use. Treat it as a starting point you own and harden, not a finished deployment. See [`DISCLAIMER.txt`](../DISCLAIMER.txt) for the full terms.

Before deploying outside a test environment, at minimum:

- **Run your own security review and threat model** of the deployed architecture — the IAM policies, network boundaries, Cognito configuration, gateway authorization, and the admin console's access control — against your organization's requirements.
- **Review the third-party application.** Open WebUI is not developed, maintained, or supported by AWS. Evaluate its security posture and release cadence yourself, and own the version you run — pin `OPEN_WEBUI_IMAGE` to a release you have validated rather than relying on the latest-release default (see [`UPGRADE_RUNBOOK.md`](UPGRADE_RUNBOOK.md)). Vulnerabilities in the upstream image are yours to track and patch.
- **Review data lifecycle defaults.** The data stores (Aurora, the S3 upload bucket, the Cognito user pool) are set to `RETAIN` with Aurora deletion protection on, so a `cdk destroy` does not take data with it — but retention is not a backup strategy. Establish backups and a tested restore path, and review Aurora backup retention, log retention, and any lifecycle rules.
- **Test at your expected scale,** including failure modes (Bedrock throttling, Aurora failover, Redis eviction, gateway or interceptor errors) and cost. See [`COST_ANALYSIS_20K_USERS.md`](COST_ANALYSIS_20K_USERS.md).
- **Configure model safeguards.** Generative AI models can return inaccurate or unexpected output. Evaluate [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) and AgentCore Policy for your use case, and decide your own data-retention and prompt-logging posture.
- **Establish operational ownership** — patching, monitoring and alerting thresholds, incident response, and a documented on-call runbook. The alarms this sample creates are a starting set, not a complete operational baseline.
- **Scan your own build.** Run dependency and container scanning in your pipeline; the sample pins versions at a point in time and does not track advisories on your behalf.

---

## Deployment

There is **one deployment path**: the standalone `./deploy.sh` script. It deploys all five CDK stacks with `cdk deploy --all` and prints the CloudFront URL.

### Deployment Process Overview

**Time to deploy:** Approximately **25–35 minutes** on first deploy (Aurora, Redis, and CloudFront are the long poles). Subsequent config-only updates take a few minutes.

`deploy.sh` automates the entire process:

1. Validates prerequisites (AWS CLI, Node.js, npm, CDK). **No Docker.**
2. Resolves the Open WebUI version (`OPEN_WEBUI_IMAGE` in `.env`, or the latest official release when unset) to an immutable image digest, then deploys the five CDK stacks in order (Network → Data → Auth → Gateway → Compute) with `cdk deploy --all`. ECS pulls the unmodified official Open WebUI image by that digest — nothing is built.
3. Populates infrastructure outputs into `.env` (Cognito IDs, OIDC config, S3 bucket, etc.). The gateway URL is injected into the ECS task directly by CDK.
4. Syncs the Cognito client secret to Secrets Manager.
5. Updates the Cognito callback URL to match the deployed CloudFront domain.
6. Forces an ECS service deployment so the task picks up the resolved configuration. At container start, [`pipe/seed.py`](../pipe/seed.py) installs the Claude pipe + two OpenAI connections into the app database.

### Step 1: Clone the Repository

```bash
git clone https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock.git
cd sample-open-webui-on-aws-with-bedrock
```

### Step 2: Configure Environment Variables

```bash
cp .env.example .env
```

Review `.env`. **No Bedrock variables are needed** — the gateway handles Bedrock, and the OAuth/infrastructure values are auto-populated by `deploy.sh` from the CDK stack outputs after the first deployment. The one setting worth confirming is left on by default:

```bash
# WebSocket stays enabled — CloudFront passes it through the VPC origin
ENABLE_WEBSOCKET_SUPPORT=true
```

**Open WebUI version (optional).** Leave `OPEN_WEBUI_IMAGE` unset to deploy the latest official Open WebUI release — `deploy.sh` resolves it to an immutable image digest at deploy time. To pin a specific version instead:

```bash
# a release tag (resolved to its digest at deploy time), or
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.11.0
# a full digest (used verbatim; works without registry access at deploy time)
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui@sha256:…
```

See [`UPGRADE_RUNBOOK.md`](UPGRADE_RUNBOOK.md) for upgrades and rollback.

### Step 3: (Optional) Configure Custom Domain

If using a custom domain, create `infra/deploy.config.json`:

```json
{
  "domainName": "oui.yourdomain.com",
  "certificateArn": "arn:aws:acm:us-east-1:ACCOUNT_ID:certificate/CERT_ID"
}
```

The ACM certificate must be in `us-east-1` (required for CloudFront). You can also supply these interactively (`deploy.sh` prompts for a domain and cert ARN) or via `--domain` / `--cert-arn` flags — the script writes them into `deploy.config.json` for you.

### Step 4: Enable Amazon Bedrock Model Access

In the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/):

1. Navigate to **Model access** in the left sidebar.
2. Click **Manage model access**.
3. Enable the models you want to use (e.g., Anthropic Claude, plus the Chat Completions / Responses models on `bedrock-mantle`).
4. Wait for access status to show "Access granted".

The gateway's capability probe and interceptor only surface models your account can actually call, so account-gated models are excluded automatically.

### Step 5: Deploy

```bash
./deploy.sh
```

Running with no flags is **interactive**: it prompts you to pick an AWS profile and region, then confirms before deploying. You can also drive it non-interactively:

```bash
# Specify profile and region explicitly
./deploy.sh --profile YOUR_AWS_PROFILE --region us-west-2

# Config-only update: push .env changes to ECS and restart, no CDK deploy
./deploy.sh --env-only --profile YOUR_AWS_PROFILE
```

If CDK isn't bootstrapped in the target account/region yet, `deploy.sh` bootstraps it (skip with `--skip-bootstrap` if already done). The script handles SSO profile credential export automatically.

### Step 6: (Optional) Configure DNS

After deployment, the script prints the CloudFront distribution domain (e.g., `d111111abcdef8.cloudfront.net`). If using a custom domain, create a CNAME record with your DNS provider:

```
oui.yourdomain.com  CNAME  d111111abcdef8.cloudfront.net
```

### Step 7: Create an Admin User in Cognito

In the [Amazon Cognito console](https://console.aws.amazon.com/cognito/):

1. Navigate to your User Pool (created by the Auth stack).
2. Click **Create user**.
3. Enter an email address and a temporary password.
4. (Optional) Add the user to the `admin` group. Note that the **first user to sign in becomes the admin** regardless of group, so the very first sign-in doesn't strictly require group membership — but adding admins to the `admin` group keeps role mapping correct for everyone after the first.
5. The user will be prompted to set a permanent password on first login.

Alternatively, enable self-registration in the Cognito User Pool settings and have users sign up, then add them to the appropriate group.

### Step 8: Verify Deployment

1. Navigate to your CloudFront URL (or custom domain) in a browser.
2. Click **Amazon Cognito** (the SSO login button) and authenticate with the user you created. The first user to sign in becomes the admin.
3. Wait about a minute — on first admin sign-in, [`pipe/seed.py`](../pipe/seed.py) installs the Claude pipe and the two OpenAI connections. The Bedrock models then appear in the model dropdown.
4. Select a model and send a test message to verify it responds. Claude models require an SSO (OAuth) session by default — see the troubleshooting notes below.

---

## Post-Deployment Configuration

### Model Access Control

Model access is managed via Open WebUI's **native RBAC** — there is no CLI script for this; you configure it in the admin UI.

1. Cognito groups (e.g., `basic-users`, `power-users`, `faculty`) sync automatically to Open WebUI groups on each login (OAuth Group Management).
2. In the admin UI, go to **Workspace → Models**.
3. Set a model's visibility to **Private** and grant access to specific groups, or leave it public for all users.

Because Cognito groups drive Open WebUI groups, you manage membership in Cognito and access-per-group in the Open WebUI admin UI. No environment variables or scripts are involved.

### Usage Visibility and Cost Control

Per-response token usage is surfaced by Open WebUI's native usage display where the provider returns usage fields (hover the info icon on an assistant message).

**Application-level token metering and quota enforcement are not deployed by default** — they are available as the opt-in metering module (`./deploy.sh --metering`): per-user token/dollar metering, operator-set quotas enforced at the gateway, and an admin console. See [`METERING.md`](METERING.md). Without the module, use AWS-native mechanisms:

- [Amazon Bedrock service quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html) — hard request/token-rate ceilings per model.
- [CloudWatch Bedrock metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring.html) — invocation counts and token usage for dashboards/alarms.
- [Application inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-create.html) — per-workload cost allocation tags.
- [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) — spend alerts on the account.

Because inbound gateway auth is `CUSTOM_JWT` with the real user identity, you can also attach [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html) (Cedar) and token-limit policies at the gateway for per-user governance (not enabled by default). See [`GATEWAY_INTEGRATION_GUIDE.md`](GATEWAY_INTEGRATION_GUIDE.md).

### Adding or Refreshing Models

New `bedrock-mantle` models don't appear until they're listed in [`config/model-capabilities.json`](../config/model-capabilities.json), the interceptor's input and the sample's single source of truth for lane membership. To refresh:

1. Run [`scripts/probe-model-capabilities.py`](../scripts/probe-model-capabilities.py), which probes every `bedrock-mantle` model against each API and excludes account-gated ones.
2. Commit the updated JSON.
3. Redeploy the Gateway stack (or `./deploy.sh` again) to refresh the interceptor.

### Additional LLM Providers (optional)

Open WebUI also supports OpenAI-compatible APIs alongside the Bedrock gateway lanes. In `.env`:

```bash
ENABLE_OPENAI_API=true
OPENAI_API_BASE_URLS=https://api.openai.com/v1
OPENAI_API_KEYS=sk-xxx
```

Then `./deploy.sh --env-only`.

### PersistentConfig Behavior

Open WebUI stores configuration in the database via `PersistentConfig`. Once a setting is saved via the Admin UI, it takes precedence over the environment variable. The seeder is idempotent and only (re)asserts the connections if absent, so admin edits to model visibility or connection settings survive redeploys. To force an env-var override, change the value in the Admin UI, or update the `config` table in Aurora directly.

---

## Operations and Monitoring

### Viewing Logs

```bash
# Tail ECS container logs
aws logs tail /ecs/open-webui --follow --profile YOUR_PROFILE --region us-east-1

# Search for errors
aws logs tail /ecs/open-webui --since 1h --profile YOUR_PROFILE --region us-east-1 \
  | grep -iE "error|exception|traceback"
```

The gateway interceptor and provisioner Lambdas have their own CloudWatch log groups (under `/aws/lambda/`) — useful when debugging model listings or target creation.

### Health Checks

- **ECS task health:** ECS console → Cluster → Service → Tasks tab.
- **ALB target health:** EC2 console → Target Groups → Targets tab.
- **Aurora:** RDS console → Databases → your cluster.
- **Redis:** ElastiCache console → Redis clusters.
- **Gateway:** Amazon Bedrock AgentCore console → Gateways.

### CloudFront Cache Invalidation

If you see stale content after a deployment:

```bash
aws cloudfront create-invalidation \
  --distribution-id YOUR_DISTRIBUTION_ID \
  --paths "/*" \
  --profile YOUR_PROFILE --region us-east-1
```

### ECS Auto-Scaling

The ECS service is configured with auto-scaling based on CPU and memory utilization. To adjust the bounds, modify the capacity settings in [`infra/lib/compute-stack.ts`](../infra/lib/compute-stack.ts) and redeploy with `./deploy.sh`.

---

## Updating the Solution

```bash
# Full deploy (all five stacks; picks up an Open WebUI version change, gateway/capability changes, infra)
./deploy.sh --profile YOUR_PROFILE

# Config-only update (push .env changes to ECS env vars and restart, no CDK)
./deploy.sh --env-only --profile YOUR_PROFILE
```

- **Changing the Open WebUI version** (upgrading to a newer release, or pinning via `OPEN_WEBUI_IMAGE`) is covered in [`UPGRADE_RUNBOOK.md`](UPGRADE_RUNBOOK.md), then `./deploy.sh`.
- **Refreshing the model catalog** — re-run the capability probe and redeploy the Gateway stack (see [Adding or Refreshing Models](#adding-or-refreshing-models)).

---

## Uninstall the Solution

To remove all deployed resources:

```bash
cd infra
npx cdk destroy --all --profile YOUR_PROFILE
# metering deployments must include the flag so the Metering stack is in scope:
npx cdk destroy --all -c metering=on --profile YOUR_PROFILE
```

**Retained resources** (removed manually if desired):

- **Aurora cluster** — Deletion protection is on and the removal policy is `RETAIN`: `cdk destroy` leaves the cluster **running (and billing)**. To remove it, disable deletion protection, then delete the cluster in the RDS console, taking a final snapshot if you want the data.
- **Cognito user pool** — Retained (`RETAIN`) with its users; delete in the Cognito console.
- **S3 upload bucket** — Must be emptied before deletion.
- **CloudWatch log groups** — Lambda log groups persist; the ECS application log group is deleted with the stack.

---

## Troubleshooting

### "Sign in with SSO" Button Not Visible

**Cause:** The SSO button appears when OIDC is configured. If the OIDC environment variables are missing from the ECS task definition, no SSO option is shown.

**Fix:** Run `./deploy.sh --env-only --profile YOUR_PROFILE` to repopulate OIDC vars from CDK outputs, then confirm the ECS task definition has `OAUTH_CLIENT_ID` and `OPENID_PROVIDER_URL`.

### SSO Login Redirects Back to the Login Page

**Cause:** The Socket.IO WebSocket can't connect end-to-end (for example, a proxy in front of the ALB that strips `Upgrade` headers, or `ENABLE_WEBSOCKET_SUPPORT` flipped off on one side only). The app runs websocket-only transport, so a broken upgrade path breaks realtime chat. Note that CloudFront VPC origins **do** support WebSocket — the stack's own CloudFront → ALB path passes upgrades through.

**Fix:** Verify a WebSocket upgrade through your URL returns HTTP 101, and that `ENABLE_WEBSOCKET_SUPPORT=true` is set in the task definition. Then `./deploy.sh --env-only --profile YOUR_PROFILE`.

### Cognito "redirect_mismatch" Error

**Cause:** The Cognito app client's callback URL doesn't match the URL Open WebUI sends. On a custom-domain deployment this can happen before the callback URL is reconciled to the final domain.

**Fix:** Re-run `./deploy.sh` (it updates the Cognito callback URLs to match the deployed CloudFront/custom domain), and confirm `WEBUI_URL` / `OPENID_REDIRECT_URI` in `.env` match your domain.

### SSO Login Returns "Email or Password Incorrect"

**Cause:** The Cognito client secret in Secrets Manager doesn't match the actual Cognito-generated secret. This can happen on first deploy before the secret sync runs.

**Fix:** `deploy.sh` syncs the secret automatically. If it failed, re-run `./deploy.sh` (or `--env-only`), which re-reads the client secret and writes it to `open-webui/cognito-client-secret`, then restarts the service.

### No Bedrock Models in the Dropdown

**Cause:** The three lanes are seeded on the **first admin sign-in**. If no one has signed in yet, or the seeder is still running, the connections/pipe aren't installed. Alternatively, the capability matrix may not include the models for your account/region.

**Fix:**
1. Confirm an admin has signed in (the first user to sign in becomes admin, which triggers `pipe/seed.py`). Wait ~1 minute after that first sign-in.
2. Check the ECS logs for the seeder: `aws logs tail /ecs/open-webui --since 5m ... | grep -i seed`.
3. Confirm your models are present in [`config/model-capabilities.json`](../config/model-capabilities.json); if not, re-run the capability probe and redeploy the Gateway stack (see [Adding or Refreshing Models](#adding-or-refreshing-models)).

### Claude Models Require SSO Login

**Cause:** The Claude pipe authenticates to the gateway with the **user's own OAuth token** and, by default, does **not** fall back to a shared identity. The `SIGV4_FALLBACK` valve is **off** by default, so a user with no OAuth session (e.g. a local-password login) gets a clear error telling them to sign in with SSO.

**Fix:** Use Cognito SSO for all users. If you accept shared-identity model calls for local-password users, turn the pipe's `SIGV4_FALLBACK` valve **on** — this lets those users fall back to the task role (SigV4 direct to Bedrock) at the cost of per-user attribution. See [`GATEWAY_INTEGRATION_GUIDE.md`](GATEWAY_INTEGRATION_GUIDE.md).

### "Model Not Found" or 400/AccessDenied When Chatting

**Cause:** The selected model may not be enabled for your account in Bedrock, or (for the Chat Completions / Responses lanes) the gateway execution role lacks `bedrock-mantle` permissions.

**Fix:**
1. Verify model access in the [Bedrock console](https://console.aws.amazon.com/bedrock/) → Model access.
2. Confirm the model is in the correct lane in [`config/model-capabilities.json`](../config/model-capabilities.json) (Claude is Messages-only, GPT-5.x is Responses-only, most others are Chat Completions).
3. Check the gateway interceptor Lambda logs and the ECS logs for the specific error.

### 504 Gateway Timeout

**Cause:** CloudFront may be caching error responses from a previous failed deployment, or the ECS service isn't yet healthy behind the ALB.

**Fix:**
1. Confirm the ECS service is stable and ALB targets are healthy.
2. Invalidate the CloudFront cache:
   ```bash
   aws cloudfront create-invalidation \
     --distribution-id YOUR_DISTRIBUTION_ID \
     --paths "/*" \
     --profile YOUR_PROFILE --region us-east-1
   ```

### Database Connection Errors on Startup

**Cause:** The container assembles its database connection from environment/secret values injected by the task definition. If the database host or password is missing, the connection fails.

**Fix:** Verify the ECS task definition has the expected database environment variables and secrets, then run `./deploy.sh --env-only --profile YOUR_PROFILE` to refresh them from CDK outputs.

---

## Related Resources

- [Gateway Integration Guide](GATEWAY_INTEGRATION_GUIDE.md) — Technical deep-dive into the AgentCore inference gateway, interceptor, capability matrix, and the Claude pipe.
- [Upgrade Runbook](UPGRADE_RUNBOOK.md) — How Open WebUI version selection, upgrades, and rollback work.
- [Cost Analysis (20K users)](COST_ANALYSIS_20K_USERS.md) — Detailed cost model at scale.
- [Open WebUI Documentation](https://docs.openwebui.com/)
- [Amazon Bedrock User Guide](https://docs.aws.amazon.com/bedrock/latest/userguide/)
- [Amazon Bedrock AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Amazon Cognito Developer Guide](https://docs.aws.amazon.com/cognito/latest/developerguide/)
- [AWS CDK Developer Guide](https://docs.aws.amazon.com/cdk/v2/guide/)
- [Amazon ECS on Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html)
- [CloudFront VPC Origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)
