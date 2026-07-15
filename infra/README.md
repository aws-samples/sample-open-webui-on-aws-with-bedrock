# Open WebUI — AWS CDK Infrastructure

AWS CDK (TypeScript) infrastructure for deploying Open WebUI on Amazon ECS with Fargate.

## Stacks

| Stack | Resources | Description |
|---|---|---|
| `OpenWebUI-Network` | VPC, subnets, NAT, VPC endpoints, security groups | Isolated network with private subnets and endpoints for S3, CloudWatch, Secrets Manager |
| `OpenWebUI-Data` | Aurora PostgreSQL Serverless v2, ElastiCache Redis, S3 | Auto-scaling database (0.5–8 ACU), Redis with TLS (CfnReplicationGroup), file storage |
| `OpenWebUI-Auth` | Cognito User Pool, client, domain, groups | SSO authentication with admin/user/power-users/basic-users groups |
| `OpenWebUI-Gateway` | AgentCore inference gateway, models-filter interceptor Lambda, inference-target custom resource, gateway execution role | Per-user (Cognito JWT) inference endpoint fronting `bedrock-mantle`; see [`GATEWAY_INTEGRATION_GUIDE.md`](../docs/GATEWAY_INTEGRATION_GUIDE.md) |
| `OpenWebUI-Compute` | ECS Fargate, internal ALB, CloudFront VPC origin, Secrets Manager | Container compute (1–10 tasks) running the unmodified official image, HTTPS via CloudFront, credential management |

**Dependency order:** Network → Data + Auth (parallel), Auth → Gateway → Compute

## Quick Start

```bash
cd infra
npm install
npx cdk bootstrap aws://ACCOUNT_ID/REGION --profile YOUR_PROFILE
npx cdk deploy --all --require-approval broadening --profile YOUR_PROFILE
```

Or use the automated deploy script from the repo root:

```bash
./deploy.sh --profile YOUR_PROFILE
```

## Container image

The Compute stack runs the **unmodified official Open WebUI image**, pinned by
digest in the `OFFICIAL_IMAGE` constant at the top of
[`lib/compute-stack.ts`](lib/compute-stack.ts) (currently v0.10.2). There is no
image build — ECS pulls the image straight from `ghcr.io/open-webui/open-webui`.
The Amazon Bedrock integration is the Gateway stack plus a pipe function and two
OpenAI connections seeded into the app at container start (see
[`../pipe/`](../pipe/)). Move the pin with
[`../docs/UPGRADE_RUNBOOK.md`](../docs/UPGRADE_RUNBOOK.md).

## Configuration

- **Domain/cert:** `infra/deploy.config.json` (gitignored) — persists across deploys
- **App config:** `.env` in repo root — source of truth for all application settings
- **CDK context:** CLI context overrides `deploy.config.json` values

### Optional CDK context flags

| Flag | Default | Effect |
|---|---|---|
| `enableModelRefresh` | `false` | Adds the scheduled model-capability refresher (Lambda + EventBridge schedule + SNS topic) to the Gateway stack. When `false`, none of these resources exist. See [the gateway guide](../docs/GATEWAY_INTEGRATION_GUIDE.md#operational-notes). |
| `modelRefreshRateHours` | `24` | Refresher cadence, in hours (only when `enableModelRefresh=true`). |

Pass with `-c`, e.g. `./deploy.sh` after `npx cdk deploy -c enableModelRefresh=true`,
or add to `cdk.context.json`. `deploy.sh` vendors the refresher's Python deps
(boto3 ≥ 1.43 + requests) only when the flag is on.

## Key Design Decisions

- **Internal ALB + CloudFront VPC origin** — ALB has no public ingress. CloudFront manages connectivity via ENIs in the VPC.
- **WebSocket over the VPC origin** — CloudFront supports WebSocket to VPC origins; the stack pins `ENABLE_WEBSOCKET_SUPPORT=true` and shares Socket.IO state across tasks via the Redis manager.
- **Redis via CfnReplicationGroup** — Required for TLS support (CfnCacheCluster doesn't support TLS).
- **DATABASE_URL composed by an ECS command override** — the official image's `start.sh` ships unpatched; the task command exports `DATABASE_URL` from component env vars (host/port/name/user + password injected from Secrets Manager) before exec'ing it.
- **Vectors in Aurora pgvector** — `VECTOR_DB=pgvector` so retrieval data survives task restarts (the default on-container Chroma store is ephemeral).

## Validation

```bash
npx tsc --noEmit          # TypeScript compilation check
npx cdk synth --quiet     # CloudFormation template generation
npx cdk diff              # Preview changes before deploy
```

## Full Documentation

See [AWS Deployment Guide](../docs/AWS_DEPLOYMENT_GUIDE.md) for complete deployment instructions, security considerations, and troubleshooting.
