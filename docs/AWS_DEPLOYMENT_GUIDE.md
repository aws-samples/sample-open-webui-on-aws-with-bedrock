# Open WebUI on AWS — Implementation Guide

This implementation guide provides an overview of deploying Open WebUI on Amazon Web Services (AWS) with native Amazon Bedrock integration, its reference architecture and components, considerations for planning the deployment, security best practices, and step-by-step configuration instructions. This guide is intended for Solutions Architects, DevOps engineers, platform engineers, and IT administrators who want to deploy a self-hosted, multi-provider generative AI chat platform on AWS.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Plan Your Deployment](#plan-your-deployment)
- [Security](#security)
- [Choose Your Deployment Path](#choose-your-deployment-path)
- [Option A: Standalone Deployment (deploy.sh)](#option-a-standalone-deployment-deploysh)
- [Option B: CI/CD Pipeline Deployment](#option-b-cicd-pipeline-deployment)
- [Post-Deployment Configuration](#post-deployment-configuration)
- [Operations and Monitoring](#operations-and-monitoring)
- [Updating the Solution](#updating-the-solution)
- [Uninstall the Solution](#uninstall-the-solution)
- [Troubleshooting](#troubleshooting)

---

## Overview

This implementation guide provides an automated AWS Cloud Development Kit (AWS CDK) deployment of [Open WebUI](https://docs.openwebui.com/) onto Amazon Elastic Container Service (Amazon ECS) with Fargate. It is pre-configured with defaults that allow most users to quickly deploy a production-ready, self-hosted AI chat platform with native Amazon Bedrock integration.

The deployed application is the **official Open WebUI release, pinned to v0.10.2**, extended at Docker-build time with this repo's small, clearly-attributed Bedrock provider (2 backend modules + 5 patches — see `patches/README.md`). The repo-root `Dockerfile` offers two targets: `backend` (default) and `full` (adds an admin Connections panel section for Bedrock; select with `-c imageTarget=full`).

### Features and Benefits

- **Native Amazon Bedrock integration** — Uses the Converse API with cross-region inference profiles for access to Claude, Nova, Llama, Mistral, and other foundation models without managing API keys or external provider accounts.
- **Enterprise SSO authentication** — Amazon Cognito integration via Open WebUI's built-in OIDC support. Group-based role mapping syncs Cognito groups to Open WebUI roles and groups on every login. Zero custom auth code.
- **Multi-provider support** — Simultaneously use Amazon Bedrock, Ollama, and OpenAI-compatible APIs. The platform normalizes all providers to a consistent chat completion format.
- **Group-based model access control** — Restrict which Bedrock models are available to which Cognito groups using Open WebUI's native RBAC.
- **Fully private networking** — Internal ALB with CloudFront VPC origin. No public-facing load balancer. All data-plane traffic stays within the VPC.
- **Serverless data tier** — Aurora PostgreSQL Serverless v2 (auto-scaling 0.5–8 ACU) and ElastiCache Redis with TLS encryption.
- **Two deployment options** — Single-command deployment via `deploy.sh` for quick setup, or a full CI/CD pipeline (CodePipeline) with separate dev/prod environments, smoke tests, and manual approval gates.
- **Built-in RAG** — Retrieval Augmented Generation with 14 vector database backends, 25+ web search providers, and multiple document loaders.

### Use Cases

- **Enterprise AI chat platform** — Provide employees with a ChatGPT-like interface backed by Amazon Bedrock models, with SSO authentication and usage controls.
- **Multi-model evaluation** — Compare responses across Claude, Nova, Llama, and other models side-by-side in a single interface.
- **RAG-powered knowledge base** — Upload documents and query them using any connected LLM with built-in retrieval augmented generation.
- **Governed AI access** — SSO-gated access with group-based model restrictions; pair with AWS-native cost controls (Bedrock service quotas, CloudWatch usage metrics, AWS Budgets — no application-level quota enforcement is included in this sample).

---

## Architecture

### Architecture Diagram

```
                           ┌──────────────────┐
                           │    End Users      │
                           └────────┬─────────┘
                                    │ HTTPS
                           ┌────────▼─────────┐
                           │   CloudFront      │
                           │   Distribution    │
                           │  (HTTPS + CDN)    │
                           └────────┬─────────┘
                                    │ VPC Origin
┌───────────────────────────────────┼──────────────────────────────────┐
│  VPC (Private Subnets)            │                                  │
│                          ┌────────▼─────────┐                        │
│                          │  Internal ALB     │                        │
│                          │  (not internet-   │                        │
│                          │   facing)         │                        │
│                          └────────┬─────────┘                        │
│                                   │                                  │
│                          ┌────────▼─────────┐                        │
│                          │  ECS Fargate      │                        │
│                          │  (Open WebUI)     │                        │
│                          └──┬─────┬──────┬──┘                        │
│                             │     │      │                           │
│              ┌──────────────┘     │      └──────────────┐            │
│              │                    │                      │            │
│     ┌────────▼──────┐   ┌────────▼──────┐   ┌──────────▼────────┐   │
│     │    Aurora      │   │  ElastiCache  │   │    S3 Bucket      │   │
│     │  PostgreSQL    │   │    Redis      │   │   (file uploads)  │   │
│     │ Serverless v2  │   │   (TLS)      │   │                   │   │
│     └───────────────┘   └──────────────┘   └───────────────────┘   │
│                                                                      │
│     ┌───────────────┐   ┌──────────────┐   ┌───────────────────┐   │
│     │   Cognito      │   │   Secrets    │   │  VPC Endpoints    │   │
│     │  User Pool     │   │   Manager    │   │ (S3, ECR, Bedrock │   │
│     │  (SSO)         │   │              │   │  CW, SM)          │   │
│     └───────────────┘   └──────────────┘   └───────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                           ┌────────▼─────────┐
                           │  Amazon Bedrock   │
                           │  (Converse API)   │
                           │  Cross-region     │
                           │  inference        │
                           └──────────────────┘
```

### Architecture Steps

1. End users access the application through an Amazon CloudFront distribution, which provides HTTPS termination, CDN caching for static assets, and optional custom domain support via AWS Certificate Manager (ACM).
2. CloudFront forwards requests to an internal Application Load Balancer (ALB) via a VPC origin. The ALB is not internet-facing — CloudFront creates ENIs in the VPC for private connectivity.
3. The ALB routes traffic to Amazon ECS Fargate tasks running the Open WebUI container. The container image is built during deployment and stored in Amazon Elastic Container Registry (ECR).
4. The application uses Amazon Bedrock's Converse API with cross-region inference profiles (e.g., `us.anthropic.claude-sonnet-4-5-*`) for LLM access. IAM roles grant the ECS task permissions to invoke models and list inference profiles.
5. Amazon Cognito provides SSO authentication. Users authenticate via the Cognito Hosted UI, and the callback auto-provisions users with role mapping based on Cognito group membership.
6. Aurora PostgreSQL Serverless v2 stores application data (users, chats, configurations, migrations). ElastiCache Redis provides session caching and shared Socket.IO state with TLS encryption. Amazon S3 stores uploaded files.
7. AWS Secrets Manager stores sensitive credentials (WEBUI_SECRET_KEY, Cognito client secret, database password). VPC endpoints provide private connectivity to AWS services without traversing the internet.

### AWS Services in This Solution

| AWS Service | Role | Description |
|---|---|---|
| Amazon CloudFront | Core | HTTPS termination, CDN, custom domain support, DDoS protection via AWS Shield Standard |
| Amazon ECS (Fargate) | Core | Serverless container compute with auto-scaling (1–10 tasks) |
| Amazon Bedrock | Core | Native LLM provider via Converse API with cross-region inference profiles |
| Amazon Cognito | Core | SSO authentication with group-based RBAC and auto-provisioning |
| Amazon VPC | Core | Isolated network with private subnets, NAT gateway, and VPC endpoints |
| Application Load Balancer | Supporting | Internal load balancing (private, accessed only via CloudFront VPC origin) |
| Aurora PostgreSQL Serverless v2 | Supporting | Auto-scaling database (0.5–8 ACU) for application data |
| Amazon ElastiCache (Redis) | Supporting | Session cache and Socket.IO state sharing with TLS encryption |
| Amazon S3 | Supporting | File upload storage |
| Amazon ECR | Supporting | Container image registry |
| AWS Secrets Manager | Supporting | Secure storage for credentials and API keys |
| AWS Certificate Manager (ACM) | Security | TLS certificates for custom domain (optional) |
| Amazon CloudWatch | Monitoring | Container logs, metrics, and alarms |
| AWS IAM | Security | Least-privilege access for ECS tasks, Bedrock invocation, and service roles |

---

## Plan Your Deployment

### Prerequisites

Before deploying, ensure you have:

1. **AWS Account** with permissions to create IAM roles, VPCs, ECS clusters, CloudFront distributions, Cognito user pools, Aurora clusters, ElastiCache clusters, and S3 buckets.
2. **AWS CLI v2** installed and configured with credentials (SSO profiles supported).
3. **Node.js 18–22** (Node 24 is not compatible). If using nvm: `nvm install 22 && nvm use 22`.
4. **Docker** installed and running (for building the container image).
5. **Amazon Bedrock model access** enabled in your target region. Go to the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/) → Model access → Enable the models you want to use.
6. **(Optional) Custom domain** — An ACM certificate in `us-east-1` for your domain. You will create a DNS CNAME record pointing to the CloudFront distribution after deployment.

### Cost

The following table provides a sample cost breakdown for deploying this solution with default parameters in `us-east-1` for one month. LLM provider costs (Amazon Bedrock token usage) are not included as they vary by usage.

| AWS Service | Dimensions | Cost/Month (USD) |
|---|---|---|
| Amazon ECS (Fargate) | 1 task, 1 vCPU, 2 GB memory, 24/7 | ~$35 |
| Amazon CloudFront | 1 distribution, 100 GB transfer, 1M requests | ~$10 |
| Aurora PostgreSQL Serverless v2 | 0.5 ACU minimum, light usage | ~$45 |
| Amazon ElastiCache (Redis) | 1 cache.t3.micro node | ~$13 |
| Amazon VPC | 1 NAT Gateway, 10 GB data processed | ~$35 |
| Application Load Balancer | 1 ALB, light traffic | ~$18 |
| Amazon S3 | 10 GB storage | ~$1 |
| Amazon Cognito | Up to 50,000 MAU (free tier) | $0 |
| AWS Secrets Manager | 3 secrets | ~$2 |
| Amazon ECR | 2 GB image storage | ~$1 |
| Amazon CloudWatch | Log storage and basic metrics | ~$5 |
| ACM | 1 certificate | Free |
| **TOTAL** | | **~$165/month** |

Costs scale with usage. Aurora Serverless v2 scales to 0.5 ACU during idle periods. ECS auto-scaling adds tasks (up to 10) under load. We recommend creating a [budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) through AWS Cost Explorer to monitor spending.

### Known Limitations

- **WebSocket is required end-to-end.** CloudFront supports WebSocket over VPC origins, and the stack pins `ENABLE_WEBSOCKET_SUPPORT=true` (Socket.IO runs websocket-only, with Redis-shared state across tasks). If you front the ALB with something other than this stack's CloudFront distribution, it must pass WebSocket upgrades through.
- **DNS is managed externally.** The CDK stacks do not create Route 53 records. If using a custom domain, you must create a CNAME record pointing to the CloudFront distribution domain after deployment.
- **Single-region deployment.** The CDK stacks deploy to a single AWS region. Cross-region inference profiles provide multi-region model access without additional infrastructure.

### Supported AWS Regions

This solution can be deployed in any AWS region that supports all required services (notably Amazon Bedrock and CloudFront VPC origins). It has been tested in:

| Region Name | Region Code |
|---|---|
| US East (N. Virginia) | us-east-1 |
| US East (Ohio) | us-east-2 |
| US West (Oregon) | us-west-2 |

Amazon Bedrock cross-region inference profiles (e.g., `us.*` prefixed models) automatically route to available regions.

---

## Security

When you build systems on AWS infrastructure, security responsibilities are shared between you and AWS. This [shared responsibility model](https://aws.amazon.com/compliance/shared-responsibility-model/) reduces your operational burden. For more information, visit [AWS Cloud Security](https://aws.amazon.com/security/).

### Network Security

- **Private ALB** — The Application Load Balancer is internal (not internet-facing). It has no public ingress rules. Only CloudFront can reach it via VPC origin ENIs.
- **Private subnets** — All compute (ECS), database (Aurora), and cache (Redis) resources run in private subnets with no direct internet access.
- **VPC endpoints** — Private connectivity to S3, ECR, Bedrock, CloudWatch, and Secrets Manager without traversing the public internet.
- **NAT Gateway** — Outbound internet access (for Ollama/OpenAI providers, if configured) routes through a NAT Gateway in a public subnet.

### Data Protection

- **Encryption in transit** — All connections use TLS: CloudFront → ALB (HTTP within VPC), Redis (`rediss://` scheme), Aurora (SSL enforced), S3 (HTTPS).
- **Encryption at rest** — Aurora PostgreSQL, ElastiCache Redis, S3, and ECS ephemeral storage use AWS-managed encryption keys.
- **Secrets Manager** — WEBUI_SECRET_KEY, Cognito client secret, and database password are stored in AWS Secrets Manager and injected into the ECS task definition at runtime. They are never stored in `.env` files or source code.

### Identity and Access Management

- **ECS task role** — Follows least-privilege. Grants `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`, `bedrock:ListInferenceProfiles`, `bedrock:GetInferenceProfile` on `foundation-model/*` and `inference-profile/*` resources. S3 access scoped to the deployment bucket. Secrets Manager access scoped to deployment secrets.
- **Cognito SSO** — Users authenticate via Cognito Hosted UI. Group membership determines role (admin vs. user). No local passwords are stored for SSO users.
- **Group-based model access** — Bedrock models can be restricted per Cognito group using fnmatch patterns (e.g., `us.anthropic.claude-*` for the `power-users` group).

### Authentication Flow

Authentication uses Open WebUI's **built-in OIDC support** — Cognito is configured as a standard OIDC provider via environment variables. Zero custom auth code.

1. User clicks "Amazon Cognito" on the login page → redirected to Cognito Managed Login UI.
2. User authenticates (Cognito user pool credentials, or federated identity provider if configured).
3. Cognito redirects to `/oauth/oidc/callback` with an authorization code.
4. Open WebUI's built-in OAuth handler exchanges the code for tokens using the client secret from Secrets Manager.
5. User is auto-provisioned (or updated) based on the ID token claims.
6. Cognito groups are synced to Open WebUI groups (`ENABLE_OAUTH_GROUP_MANAGEMENT=true`).
7. Role mapping: users in `admin`/`webui-admins`/`admins` Cognito groups → admin role, all others → user role (`OAUTH_ROLES_CLAIM=cognito:groups`).
8. A session JWT is issued and the user is redirected to the application.

---

## Choose Your Deployment Path

| | Option A: Standalone | Option B: CI/CD Pipeline |
|---|---|---|
| **Best for** | Quick setup, single environment, demos | Production teams, multi-environment, continuous delivery |
| **Environments** | Single (default stack names) | Dev + Prod (isolated stacks) |
| **Deploy method** | `./deploy.sh` from your laptop | CodePipeline triggered by `git push` |
| **Docker build** | Local Docker daemon | CodeBuild (no local Docker needed) |
| **Config source** | `.env` file | CDK context + Secrets Manager |
| **Auth setup** | Auto-configured by deploy script | Auto-configured by CDK + post-deploy sync |
| **Custom domain** | `deploy.config.json` | Pipeline stack props from `deploy.config.json` |
| **Time to deploy** | ~20–30 min (first deploy) | ~40 min (first deploy, includes pipeline setup) |
| **Ongoing updates** | `./deploy.sh --skip-cdk` | `git push` to `main` |

Both paths deploy the same architecture (CloudFront → ALB → ECS Fargate → Bedrock). Choose based on your operational needs.

---

## Option A: Standalone Deployment (deploy.sh)

### Deployment Process Overview

**Time to deploy:** Approximately 20–30 minutes (first deploy). Subsequent code-only updates take ~5 minutes.

The deployment script (`deploy.sh`) automates the entire process:
1. Validates prerequisites (AWS CLI, Docker, Node.js, CDK)
2. Deploys 4 CDK stacks (Network → Data + Auth → Compute)
3. Builds the Docker image and pushes to ECR
4. Populates infrastructure outputs into `.env` (Cognito IDs, OIDC config, S3 bucket, Redis URL)
5. Syncs the Cognito client secret to Secrets Manager
6. Updates the Cognito callback URL to match the deployed CloudFront domain
7. Forces an ECS service deployment with the new image

### Step 1: Clone the Repository

```bash
git clone https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock.git
cd sample-open-webui-on-aws-with-bedrock
```

### Step 2: Install CDK Dependencies

```bash
cd infra
npm install
cd ..
```

### Step 3: Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` with your configuration. At minimum, set:

```bash
# Required
ENABLE_BEDROCK_API=true
BEDROCK_REGION=us-east-1

# Authentication via built-in OIDC (auto-populated by deploy.sh)
# OAUTH_CLIENT_ID, OPENID_PROVIDER_URL, OPENID_REDIRECT_URI, etc.

# WebSocket stays enabled — CloudFront passes it through the VPC origin
ENABLE_WEBSOCKET_SUPPORT=true
```

The deploy script auto-populates infrastructure values (Cognito IDs, S3 bucket name, Redis URL, etc.) from CDK stack outputs after the first deployment.

### Step 4: (Optional) Configure Custom Domain

If using a custom domain, create `infra/deploy.config.json`:

```json
{
  "domainName": "oui.yourdomain.com",
  "certificateArn": "arn:aws:acm:us-east-1:ACCOUNT_ID:certificate/CERT_ID"
}
```

The ACM certificate must be in `us-east-1` (required for CloudFront). For the standalone path, `domainName` and `certificateArn` are the correct keys (no environment prefix needed since there's only one environment).

### Step 5: Bootstrap CDK (First Time Only)

```bash
cd infra
npx cdk bootstrap aws://ACCOUNT_ID/us-east-1 --profile YOUR_PROFILE
cd ..
```

### Step 6: Enable Amazon Bedrock Model Access

In the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/):
1. Navigate to **Model access** in the left sidebar.
2. Click **Manage model access**.
3. Enable the models you want to use (e.g., Anthropic Claude, Amazon Nova, Meta Llama).
4. Wait for access status to show "Access granted".

Cross-region inference profiles (e.g., `us.anthropic.claude-sonnet-4-5-*`) require model access in all regions included in the profile.

### Step 7: Deploy

```bash
# Full deployment (infrastructure + application)
./deploy.sh --profile YOUR_AWS_PROFILE

# Code-only update (skip CDK, rebuild Docker image)
./deploy.sh --skip-cdk --profile YOUR_AWS_PROFILE

# Config-only update (update ECS env vars from .env, no rebuild)
./deploy.sh --env-only --profile YOUR_AWS_PROFILE
```

The script handles SSO profile credential export automatically.

### Step 8: (Optional) Configure DNS

After deployment, the script outputs the CloudFront distribution domain (e.g., `d111111abcdef8.cloudfront.net`).

If using a custom domain, create a CNAME record with your DNS provider:

```
oui.yourdomain.com  CNAME  d111111abcdef8.cloudfront.net
```

### Step 9: Create Admin User in Cognito

In the [Amazon Cognito console](https://console.aws.amazon.com/cognito/):
1. Navigate to your User Pool (created by the Auth stack).
2. Click **Create user**.
3. Enter email address and a temporary password.
4. Add the user to the `admin` group.
5. The user will be prompted to set a permanent password on first login.

Alternatively, enable self-registration in the Cognito User Pool settings and have users sign up, then manually add them to the appropriate group.

### Step 10: Verify Deployment

1. Navigate to your CloudFront URL (or custom domain) in a browser.
2. Click **Amazon Cognito** (the SSO login button).
3. Authenticate with the Cognito user you created.
4. Select a Bedrock model from the model dropdown.
5. Send a test message to verify the model responds.

---

## Option B: CI/CD Pipeline Deployment

The CI/CD pipeline automates build, test, and deploy across separate dev and prod environments. Pushes to `main` trigger the full pipeline automatically.

For the complete CI/CD guide with detailed architecture, environment isolation, migration instructions, and troubleshooting, see the [CI/CD Pipeline Guide](CICD_DEPLOYMENT_GUIDE.md).

### Pipeline Flow

```
GitHub Push (main) → Build (Docker + CDK synth) → Deploy Dev → Smoke Test + Approval → Deploy Prod
```

### Prerequisites (in addition to the common prerequisites above)

1. **GitHub repository** with a [CodeStar Connection](https://docs.aws.amazon.com/codepipeline/latest/userguide/connections-github.html) configured.
2. **CDK bootstrapped** in your target account/region.
3. No local Docker required — CodeBuild handles image builds.

### Step 1: Create CodeStar Connection

In the [CodePipeline console](https://console.aws.amazon.com/codesuite/settings/connections), create a connection to your GitHub repository. Note the connection ARN.

### Step 2: Configure deploy.config.json

```json
{
  "connectionArn": "arn:aws:codeconnections:us-east-1:ACCOUNT:connection/CONN_ID",
  "approvalEmail": "your-email@example.com",
  "prodDomainName": "oui.yourdomain.com",
  "prodCertificateArn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID"
}
```

> **Note:** Use environment-scoped keys (`prodDomainName`, `prodCertificateArn`). For a dev custom domain, use `devDomainName` and `devCertificateArn`. The `devUrl` is added after the first pipeline run.

### Step 3: Deploy the Pipeline Stack

```bash
cd infra
npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

This creates the ECR repository, CodePipeline, and all CodeBuild projects. Confirm the SNS subscription email for approval notifications.

### Step 4: Trigger the Pipeline

```bash
git push origin main
```

The first run deploys all infrastructure from scratch (~30–40 min). The dev environment deploys first, then smoke tests run, then manual approval is required before prod deploys.

### Step 5: Complete First-Time Setup

After the first successful dev deployment, get the dev CloudFront domain and update `deploy.config.json`:

```bash
aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Compute \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text --profile YOUR_PROFILE --region us-east-1
```

Add `devUrl` to `deploy.config.json`:

```json
{
  "connectionArn": "...",
  "approvalEmail": "...",
  "devUrl": "d1234abcdef.cloudfront.net",
  "prodDomainName": "oui.yourdomain.com",
  "prodCertificateArn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID"
}
```

Redeploy the pipeline stack to bake these values into the CodeBuild projects:

```bash
npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

### Step 6: Configure DNS

Create a DNS record pointing your custom domain to the prod CloudFront distribution:

```
oui.yourdomain.com  →  CNAME (or Route 53 alias)  →  d5678xyz.cloudfront.net
```

### Step 7: Create Users in Cognito

Each environment has its own Cognito User Pool. Create users and assign them to groups (`admin`, `user`, `power-users`, etc.) in the appropriate User Pool via the Cognito console.

### What the Pipeline Handles Automatically

- Docker image build and ECR push (immutable commit hash tags)
- CDK infrastructure deployment with environment-specific config
- Cognito client secret sync to Secrets Manager (post-deploy step)
- OIDC callback URL configuration via CDK context
- Custom domain and certificate configuration for CloudFront

---

## Post-Deployment Configuration

### Bedrock Model Access Control

Model access is managed via Open WebUI's native RBAC system. Cognito groups are synced to Open WebUI groups on each login, and admins grant model access to groups via the admin UI.

**Setup:**
1. Cognito groups (e.g., `basic-users`, `power-users`, `faculty`) sync automatically to Open WebUI groups via OAuth Group Management.
2. In the admin UI, go to **Workspace → Models**, set model visibility to **Private**, and grant access to specific groups.
3. For bulk operations, use the provided script:

```bash
# Grant basic-users access to Nova models only
./scripts/set-model-access.sh --url https://YOUR_DOMAIN --token $TOKEN \
    --group basic-users --pattern "us.amazon.nova*"

# Grant power-users access to ALL Bedrock models
./scripts/set-model-access.sh --url https://YOUR_DOMAIN --token $TOKEN \
    --group power-users --pattern "*"
```

Apply changes: `./deploy.sh --env-only --profile YOUR_PROFILE`

### Usage Visibility and Cost Control

Per-response token usage is emitted by the Bedrock provider in OpenAI-compatible
fields, so Open WebUI's native usage display works unchanged (hover the info
icon on an assistant message).

Application-level token metering and quota enforcement are **not included in
this sample**. For cost control, use AWS-native mechanisms:

- [Amazon Bedrock service quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html) — hard request/token-rate ceilings per model.
- [CloudWatch Bedrock metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring.html) — invocation counts and token usage for dashboards/alarms.
- [Application inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-create.html) — per-workload cost allocation tags.
- [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) — spend alerts on the account.

### Additional LLM Providers

Open WebUI supports Ollama and OpenAI-compatible APIs alongside Bedrock:

```bash
# OpenAI
ENABLE_OPENAI_API=true
OPENAI_API_BASE_URLS=https://api.openai.com/v1
OPENAI_API_KEYS=sk-xxx

# Ollama (requires network connectivity to Ollama server)
ENABLE_OLLAMA_API=true
OLLAMA_BASE_URLS=http://ollama-host:11434
```

### PersistentConfig Behavior

Open WebUI stores configuration in the database via `PersistentConfig`. Once a setting is saved via the Admin UI, it takes precedence over the environment variable. To force an env var override, you must either:
- Change the value in the Admin UI, or
- Connect to the Aurora database and update the `config` table directly.

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

### Health Checks

- **ECS task health:** Check the ECS console → Cluster → Service → Tasks tab.
- **ALB target health:** Check the EC2 console → Target Groups → Targets tab.
- **Aurora:** Check the RDS console → Databases → your cluster.
- **Redis:** Check the ElastiCache console → Redis clusters.

### CloudFront Cache Invalidation

If you see stale content after a deployment:

```bash
aws cloudfront create-invalidation \
  --distribution-id YOUR_DISTRIBUTION_ID \
  --paths "/*" \
  --profile YOUR_PROFILE --region us-east-1
```

### ECS Auto-Scaling

The ECS service is configured with auto-scaling (1–10 tasks). Scaling is based on CPU and memory utilization. To adjust:

- Modify `minCapacity` and `maxCapacity` in `infra/lib/compute-stack.ts`.
- Redeploy: `./deploy.sh --profile YOUR_PROFILE`

---

## Updating the Solution

### Standalone (deploy.sh)

```bash
# Application update (rebuild Docker image, skip CDK infrastructure)
./deploy.sh --skip-cdk --profile YOUR_PROFILE

# Configuration update (update .env, push new env vars to ECS)
./deploy.sh --env-only --profile YOUR_PROFILE

# Full deploy (infrastructure + application)
./deploy.sh --profile YOUR_PROFILE
```

### CI/CD Pipeline

```bash
# Application or infrastructure update — just push to main
git push both main
```

The pipeline automatically builds, deploys to dev, runs smoke tests, waits for approval, and deploys to prod. For pipeline infrastructure changes (new props, buildspec updates), redeploy the pipeline stack:

```bash
cd infra && npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

---

## Uninstall the Solution

To remove all deployed resources:

```bash
cd infra
npx cdk destroy --all --profile YOUR_PROFILE
```

**Retained resources** (must be deleted manually if desired):
- **ECR repository** — Has `RemovalPolicy.RETAIN` to prevent accidental image deletion.
- **S3 bucket** — Must be emptied before deletion.
- **Aurora snapshots** — Final snapshot is created on deletion by default.
- **CloudWatch log groups** — Retained for troubleshooting.

---

## Troubleshooting

### "Sign in with SSO" Button Not Visible

**Cause:** The SSO button appears when OIDC is configured. If the OIDC environment variables are missing from the ECS task definition, no SSO option is shown.

**Fix (standalone):** Run `./deploy.sh --env-only --profile YOUR_PROFILE` to repopulate OIDC vars from CDK outputs.

**Fix (pipeline):** Verify the Compute stack has the OIDC env vars. Check the ECS task definition for `OAUTH_CLIENT_ID` and `OPENID_PROVIDER_URL`. If missing, the CDK code may need updating — see `infra/lib/compute-stack.ts`.

### SSO Login Redirects Back to Login Page

**Cause:** the Socket.IO WebSocket can't connect end-to-end (for example, a proxy in front of the ALB that strips `Upgrade` headers, or `ENABLE_WEBSOCKET_SUPPORT` flipped off on one side only). The app runs websocket-only transport, so a broken upgrade path breaks realtime chat.

**Fix:** verify a WebSocket upgrade through your URL returns HTTP 101 (see `buildspec-smoke.yml` for the exact `curl --http1.1` probe), and that `ENABLE_WEBSOCKET_SUPPORT=true` is set in the task definition. Then `./deploy.sh --env-only --profile YOUR_PROFILE`.

### "Model Not Found" When Chatting

**Cause:** Bedrock models may not be enabled in your account, or the ECS task role may lack permissions.

**Fix:**
1. Verify model access in the [Bedrock console](https://console.aws.amazon.com/bedrock/) → Model access.
2. Check ECS task role has `bedrock:InvokeModel` and `bedrock:ListInferenceProfiles` permissions.
3. Check logs: `aws logs tail /ecs/open-webui --since 5m --profile YOUR_PROFILE --region us-east-1`

### Database Connection Errors on Startup

**Cause:** `start.sh` constructs `DATABASE_URL` from component environment variables at container startup. If `DATABASE_HOST` or `DATABASE_PASSWORD` are missing, the connection fails.

**Fix:** Verify the ECS task definition has the correct environment variables and secrets. Run `./deploy.sh --env-only --profile YOUR_PROFILE` to refresh from CDK outputs.

### 504 Gateway Timeout

**Cause:** CloudFront may be caching error responses from a previous failed deployment.

**Fix:**
```bash
aws cloudfront create-invalidation \
  --distribution-id YOUR_DISTRIBUTION_ID \
  --paths "/*" \
  --profile YOUR_PROFILE --region us-east-1
```

### npm ci Fails During Docker Build

**Cause:** `package-lock.json` was generated with a different Node.js version or platform than the Docker build environment (`public.ecr.aws/docker/library/node:22-alpine3.20`).

**Fix:** Regenerate the lockfile inside the same container:
```bash
docker run --rm -v $(pwd):/app -w /app public.ecr.aws/docker/library/node:22-alpine3.20 \
  sh -c "rm -rf node_modules package-lock.json && npm install --force"
sudo rm -rf node_modules
```

### Pipeline: Docker Hub Rate Limit (429 Too Many Requests)

**Cause:** CodeBuild pulls base images from Docker Hub, which rate-limits unauthenticated pulls.

**Fix:** The Dockerfile should use ECR Public mirrors instead of Docker Hub:
```dockerfile
FROM public.ecr.aws/docker/library/node:22-alpine3.20 AS build
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS base
```

### Pipeline: ECS Service Fails to Stabilize

**Cause:** The new container image failed to build or push during `cdk deploy`, or the task failed its health check after startup. With the deployment circuit breaker enabled (see `ComputeStack`), the service auto-rolls back to the previous task definition on failure.

**Fix:**
1. Check the CodeBuild logs for the `Deploy-Dev` or `Deploy-Prod` stage. Look for `docker build` or `docker push` errors — most commonly a `privileged: true` missing on the CodeBuild environment, or an outdated CDK bootstrap version (needs `image-publishing-role`).
2. Check the ECS service events: `aws ecs describe-services --cluster <env>-open-webui-cluster --services <service> --query 'services[0].events[0:10]'`. If you see `circuit breaker triggered`, the new task revision never reached a steady state — inspect the task stop reason and container logs in CloudWatch.
3. Confirm the bootstrap stack is current: `aws cloudformation describe-stack-resource --stack-name CDKToolkit --logical-resource-id ImagePublishingRole`. Re-run `cdk bootstrap` if missing.

### Pipeline: Cognito "redirect_mismatch" Error

**Cause:** The Cognito app client's callback URL doesn't match the URL Open WebUI sends. This happens when `devUrl` isn't configured in `deploy.config.json` or the pipeline stack hasn't been redeployed after adding it.

**Fix:** Add `devUrl` to `deploy.config.json` and redeploy the pipeline stack:
```bash
cd infra && npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

### Pipeline: SSO Login Returns "Email or Password Incorrect"

**Cause:** The Cognito client secret in Secrets Manager doesn't match the actual Cognito-generated secret. This happens on first deploy before the post-deploy secret sync runs.

**Fix:** The pipeline's post-deploy step syncs the secret automatically. If it failed, sync manually:
```bash
POOL_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Auth \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Auth \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" --output text)
CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id $POOL_ID --client-id $CLIENT_ID \
  --query "UserPoolClient.ClientSecret" --output text)
aws secretsmanager put-secret-value \
  --secret-id open-webui/dev-cognito-client-secret --secret-string "$CLIENT_SECRET"
# Then restart ECS tasks:
aws ecs update-service --cluster dev-open-webui-cluster \
  --service SERVICE_NAME --force-new-deployment
```

### Pipeline: Prod CloudFront Missing Custom Domain

**Cause:** `deploy.config.json` is gitignored, so the pipeline doesn't have it. The `prodDomainName` and `prodCertificateArn` must be passed as pipeline stack props.

**Fix:** Add them to `deploy.config.json` and redeploy the pipeline stack:
```bash
cd infra && npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

---

## Related Resources

- [CI/CD Pipeline Guide](CICD_DEPLOYMENT_GUIDE.md) — Detailed guide for the multi-environment pipeline deployment
- [Bedrock Integration Guide](BEDROCK_INTEGRATION_GUIDE.md) — Technical deep-dive into the native Bedrock integration pattern
- [Open WebUI Documentation](https://docs.openwebui.com/)
- [Amazon Bedrock User Guide](https://docs.aws.amazon.com/bedrock/latest/userguide/)
- [Amazon Bedrock Cross-Region Inference](https://aws.amazon.com/blogs/machine-learning/getting-started-with-cross-region-inference-in-amazon-bedrock/)
- [Amazon Cognito Developer Guide](https://docs.aws.amazon.com/cognito/latest/developerguide/)
- [AWS CDK Developer Guide](https://docs.aws.amazon.com/cdk/v2/guide/)
- [Amazon ECS on Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html)
- [CloudFront VPC Origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)
