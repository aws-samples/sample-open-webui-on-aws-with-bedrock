# Open WebUI — AWS CDK Infrastructure

AWS CDK (TypeScript) infrastructure for deploying Open WebUI on Amazon ECS with Fargate.

## Stacks

| Stack | Resources | Description |
|---|---|---|
| `OpenWebUI-Network` | VPC, subnets, NAT, VPC endpoints, security groups | Isolated network with private subnets and endpoints for S3, ECR, Bedrock, CloudWatch, Secrets Manager |
| `OpenWebUI-Data` | Aurora PostgreSQL Serverless v2, ElastiCache Redis, S3 | Auto-scaling database (0.5–8 ACU), Redis with TLS (CfnReplicationGroup), file storage |
| `OpenWebUI-Auth` | Cognito User Pool, client, domain, groups | SSO authentication with admin/user/power-users/basic-users groups |
| `OpenWebUI-Compute` | ECS Fargate, internal ALB, CloudFront VPC origin, Secrets Manager | Container compute (1–10 tasks), HTTPS via CloudFront, credential management |

**Dependency order:** Network → Data + Auth (parallel) → Compute

### Supporting Construct

- `BedrockAccessConstruct` — IAM policies for Bedrock Converse API, inference profile discovery, and cross-region invocation.

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

## Container image selection

The Compute stack supports two image modes (context keys or `environment-config.ts`):

- **`imageSource=build` (default)** — CDK builds the repo-root overlay Dockerfile
  (official Open WebUI pinned release + the Bedrock provider) as a
  `DockerImageAsset` at deploy time and pushes it to the CDK asset repo.
  Choose the Dockerfile target with `-c imageTarget=backend` (default; official
  UI unchanged) or `-c imageTarget=full` (rebuilds the UI with the admin
  Connections Bedrock panel — slower build).
- **`imageSource=registry`** — escape hatch pulling a **prebuilt overlay image
  from your own ECR repo** (`-c imageRegistry=<repo-name> -c imageTag=<tag>`).
  Note the semantics: the official ghcr.io image alone has **no Bedrock
  provider** — point this at an image you built from this repo's Dockerfile
  (e.g. in CI), not at ghcr.io directly.

```bash
# Example: deploy the full target
npx cdk deploy --all -c environment=dev -c imageTarget=full

# Example: deploy a prebuilt image from your ECR
npx cdk deploy --all -c environment=dev -c imageSource=registry \
  -c imageRegistry=my-openwebui-overlay -c imageTag=v0.10.2-bedrock
```

## Configuration

- **Domain/cert:** `infra/deploy.config.json` (gitignored) — persists across deploys
- **App config:** `.env` in repo root — source of truth for all application settings
- **CDK context:** CLI context overrides `deploy.config.json` values

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
