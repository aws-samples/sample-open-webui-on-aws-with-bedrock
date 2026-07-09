# Open WebUI on AWS — CI/CD Pipeline Guide

This guide covers the automated CI/CD pipeline for Open WebUI on AWS. The pipeline uses AWS CodePipeline, CodeBuild, and CDK to provide continuous deployment with separate dev and prod environments, automated smoke testing, and manual approval gates.

The pipeline is fully self-contained and can be deployed into a fresh AWS account — no prior manual deployment is required. If you have an existing deployment via `deploy.sh`, the pipeline operates independently and does not affect it. For manual deployment instructions, see the [AWS Deployment Guide](AWS_DEPLOYMENT_GUIDE.md).

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How It Works](#how-it-works)
- [Environment Isolation](#environment-isolation)
- [Prerequisites](#prerequisites)
- [Deploy the Pipeline](#deploy-the-pipeline)
- [Pipeline Operations](#pipeline-operations)
- [Relationship to Manual Deployment](#relationship-to-manual-deployment)
- [Migrating from Manual to Pipeline](#migrating-from-manual-to-pipeline)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)

---

## Overview

The CI/CD pipeline automates the build-test-deploy cycle for Open WebUI. When code is pushed to the `main` branch on GitHub, the pipeline:

1. Builds a Docker image and pushes it to ECR
2. Validates CDK templates via `cdk synth`
3. Deploys to the dev environment
4. Runs smoke tests against dev
5. Waits for manual approval
6. Deploys to the prod environment

Dev and prod are separate CDK stack sets in the same AWS account, each with their own VPC, database, Cognito user pool, and ECS service.

---

## Architecture

### Pipeline Flow

```
GitHub Push (main)
    │
    ▼
CodePipeline (V2)
    │
    ├─ Source ──────── CodeStar Connection → GitHub (aws-samples/sample-open-webui-on-aws-with-bedrock)
    │
    ├─ Deploy Dev ──── CodeBuild → cdk deploy --all -c environment=dev
    │                   ├── Builds + pushes container image via DockerImageAsset
    │                   │   (referenced in CFN by immutable SHA256 digest)
    │                   ├── OpenWebUI-Dev-Network
    │                   ├── OpenWebUI-Dev-Data
    │                   ├── OpenWebUI-Dev-Auth
    │                   └── OpenWebUI-Dev-Compute
    │
    ├─ Test & Approve
    │   ├── Smoke Test ── CodeBuild (runOrder 1)
    │   │                   ├── GET /health → 200
    │   │                   └── GET /api/v1/bedrock/models → 200
    │   │
    │   └── Approval ──── Manual approval via SNS email (runOrder 2)
    │
    └─ Deploy Prod ─── CodeBuild → cdk deploy --all -c environment=prod
                        (same source tree ⇒ same asset hash ⇒ image cache hit,
                         no rebuild; prod deploys the exact digest dev tested)
                        ├── OpenWebUI-Prod-Network
                        ├── OpenWebUI-Prod-Data
                        ├── OpenWebUI-Prod-Auth
                        └── OpenWebUI-Prod-Compute
```

### Infrastructure Created by the Pipeline Stack

| Resource | Purpose |
|----------|---------|
| ECR Repository | Container image registry (`open-webui`), shared by dev and prod |
| CodePipeline (V2) | Orchestrates the 5-stage workflow |
| CodeBuild — Build | Docker build, ECR push, CDK synth |
| CodeBuild — Deploy Dev | `cdk deploy` with `-c environment=dev` |
| CodeBuild — Smoke Test | Health and API endpoint checks |
| CodeBuild — Deploy Prod | `cdk deploy` with `-c environment=prod` |
| S3 Artifact Bucket | Pipeline artifacts (encrypted, 30-day lifecycle) |
| SNS Topic | Approval notification emails |
| IAM Roles | Scoped permissions for CodeBuild and CDK deploy |

---

## How It Works

### Deploy Stages

Both dev and prod deploy stages run `cdk deploy --all -c environment=<env> --require-approval never` via CodeBuild. Each deploy:

1. Computes a content-addressable asset hash from the Dockerfile + build context (see `ComputeStack`'s `DockerImageAsset`).
2. If the hash is not already in the CDK bootstrap-managed asset ECR repo, runs `docker build` inside CodeBuild (requires `privileged: true`) and pushes the result.
3. Writes the resulting immutable image digest into the ECS task definition.
4. Lets CloudFormation apply the stack updates.

Because the asset hash is deterministic from the source tree, and both dev and prod stages run against the same `sourceOutput` artifact, **prod computes the same hash as dev, finds the image already in ECR, and skips the rebuild** — prod deploys bit-for-bit the exact image dev tested.

The `--require-approval never` flag is safe because the pipeline's manual approval gate provides the human checkpoint between dev and prod.

The git commit SHA is propagated into the image via `GIT_COMMIT=$CODEBUILD_RESOLVED_SOURCE_VERSION`, which the `DockerImageAsset` passes through as the `BUILD_HASH` build arg. Inside the container this becomes `WEBUI_BUILD_VERSION`.

### Deployment Circuit Breaker

The `ComputeStack` enables ECS deployment circuit breaker with automatic rollback. If a new task revision fails to stabilize (bad image, failing health check, misconfigured env var), ECS reverts to the previous revision automatically — the service does not get stuck.

### Smoke Tests

After dev deployment, the smoke test stage waits 60 seconds for the ECS task to stabilize, then checks:

- `GET https://<dev-url>/health` → expects HTTP 200
- `GET https://<dev-url>/api/v1/bedrock/models` → expects HTTP 200

If either check fails, the pipeline stops and prod is not deployed.

### Manual Approval

After smoke tests pass, an SNS notification is sent to the configured email address. The approver can:

- Review the dev environment in a browser
- Check CloudWatch logs for errors
- Approve or reject the deployment in the CodePipeline console

Rejection stops the pipeline. Approval triggers the prod deploy stage.

---

## Environment Isolation

Dev and prod are fully isolated stack sets in the same AWS account.

### What's Different Between Environments

| Aspect | Dev | Prod |
|--------|-----|------|
| Stack names | `OpenWebUI-Dev-*` | `OpenWebUI-Prod-*` |
| VPC | Separate VPC | Separate VPC |
| Aurora capacity | 0.5–2 ACU, no deletion protection | 0.5–8 ACU, deletion protection ON |
| ECS | 1 task, no auto-scaling | 1–10 tasks, auto-scaling (CPU 70%, mem 80%) |
| Cognito | Separate user pool (test users) | Separate user pool (real users) |
| CloudFront | Default CloudFront domain | Custom domain + ACM cert (if configured) |
| Container image | Same SHA256 digest (built by CDK asset) | Same SHA256 digest (cache hit from dev) |
| Deploy trigger | Automatic after approval gate upstream | Manual approval required |

### What's Shared

- **CDK asset ECR repo** — Both environments pull from the same CDK bootstrap-managed container-assets ECR repo. Images are referenced by immutable SHA256 digest, not by tag.
- **AWS account** — Same account, different CloudFormation stacks
- **CDK bootstrap** — Same bootstrap stack (which also owns the asset ECR repo)

### Resource Name Isolation

Resources that would collide in a single account are prefixed with the environment name:

| Resource | Dev | Prod | No-env (manual deploy) |
|----------|-----|------|------------------------|
| ECS cluster | `dev-open-webui-cluster` | `prod-open-webui-cluster` | `open-webui-cluster` |
| Secrets Manager | `open-webui/dev-webui-secret-key` | `open-webui/prod-webui-secret-key` | `open-webui/webui-secret-key` |
| Log group | `/ecs/dev-open-webui` | `/ecs/prod-open-webui` | `/ecs/open-webui` |
| Cognito domain | `open-webui-dev-<account>` | `open-webui-prod-<account>` | `open-webui-<account>` |

---

## Prerequisites

Before deploying the pipeline, you need:

1. **AWS Account** with permissions to create IAM roles, VPCs, ECS clusters, CloudFront distributions, Cognito user pools, Aurora clusters, ElastiCache clusters, S3 buckets, CodePipeline, and CodeBuild projects.

2. **AWS CLI v2** installed and configured with credentials (SSO profiles supported).

3. **Node.js 18–22** (Node 24 is not compatible). If using nvm: `nvm install 22 && nvm use 22`.

4. **Amazon Bedrock model access** enabled in your target region. Go to the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/) → Model access → Enable the models you want to use.

5. **CDK Bootstrap** — Required once per account/region:
   ```bash
   cd infra && npm install
   npx cdk bootstrap aws://ACCOUNT_ID/us-east-1 --profile YOUR_PROFILE
   ```

6. **CodeStar Connection to GitHub** — Create in the AWS Console:
   - Go to **Developer Tools** → **Settings** → **Connections**
   - Click **Create connection** → select **GitHub**
   - Authorize AWS to access your GitHub account
   - Copy the Connection ARN (e.g., `arn:aws:codeconnections:us-east-1:123456789012:connection/abc-123`)

7. **(Optional) Custom domain** — An ACM certificate in `us-east-1` for your production domain.

> **Note:** You do NOT need an existing manual deployment. The pipeline creates all infrastructure from scratch, including the ECR repository.

---

## Deploy the Pipeline

### Step 1: Clone and Install

```bash
git clone <repository-url>
cd open-webui/infra
npm install
```

### Step 2: Configure Pipeline Context

Add the pipeline configuration to `infra/deploy.config.json`:

```json
{
  "connectionArn": "arn:aws:codeconnections:us-east-1:ACCOUNT:connection/CONN_ID",
  "approvalEmail": "your-email@example.com"
}
```

For production with a custom domain, also add:

```json
{
  "connectionArn": "arn:aws:codeconnections:us-east-1:ACCOUNT:connection/CONN_ID",
  "approvalEmail": "your-email@example.com",
  "prodDomainName": "oui.yourdomain.com",
  "prodCertificateArn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID"
}
```

> **Important:** Use environment-scoped keys (`prodDomainName`, `prodCertificateArn`) to prevent dev from inheriting prod's domain. For a dev custom domain, use `devDomainName` and `devCertificateArn`.

The `devUrl` can be added after the first pipeline run (you'll get the dev CloudFront domain from the dev Compute stack output).

### Step 3: Deploy the Pipeline Stack

```bash
# Deploy the pipeline (creates ECR repo, CodePipeline, CodeBuild projects)
npx cdk deploy OpenWebUI-Pipeline \
  -c pipeline=true \
  -c connectionArn="arn:aws:codeconnections:us-east-1:ACCOUNT:connection/CONN_ID" \
  -c approvalEmail="your-email@example.com" \
  --profile YOUR_PROFILE
```

Or if you've added the values to `deploy.config.json`:

```bash
npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

The pipeline uses the CDK bootstrap-managed container-assets ECR repo — no named ECR repository is created or required. Ensure the target account has been bootstrapped with a current CDK bootstrap version (`cdk bootstrap aws://<account>/<region>`); the bootstrap stack provides both the asset repo and the IAM roles the pipeline uses to publish to it.

### Step 4: Confirm SNS Subscription

After deployment, check your email for an SNS subscription confirmation. Click the confirmation link to receive approval notifications.

### Step 5: Trigger the Pipeline

Push a commit to the `main` branch:

```bash
git push origin main
```

The pipeline will start automatically. Monitor progress in the [CodePipeline console](https://console.aws.amazon.com/codesuite/codepipeline/pipelines).

### Step 6: Update Dev URL for Smoke Tests

After the first dev deployment completes, get the dev CloudFront domain:

```bash
aws cloudformation describe-stacks \
  --stack-name OpenWebUI-Dev-Compute \
  --query 'Stacks[0].Outputs[?OutputKey==`DistributionDomainName`].OutputValue' \
  --output text --profile YOUR_PROFILE --region us-east-1
```

Update the smoke test project's `DEV_URL` environment variable in the pipeline stack (add `devUrl` to `deploy.config.json` and redeploy the pipeline stack), or update it directly in the CodeBuild console.

### Step 7: Complete First-Time Setup

After the first successful pipeline run, update `deploy.config.json` with the values from the deployed stacks and redeploy the pipeline stack:

```json
{
  "connectionArn": "arn:aws:codeconnections:us-east-1:ACCOUNT:connection/CONN_ID",
  "approvalEmail": "your-email@example.com",
  "devUrl": "d1234abcdef.cloudfront.net",
  "prodDomainName": "oui.yourdomain.com",
  "prodCertificateArn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID"
}
```

Then redeploy the pipeline stack to bake these values into the CodeBuild projects:

```bash
cd infra && npx cdk deploy OpenWebUI-Pipeline -c pipeline=true --profile YOUR_PROFILE
```

This is needed because `deploy.config.json` is gitignored — the pipeline's CodeBuild environment doesn't have it. The pipeline stack reads the values at deploy time and passes them as environment variables to the CodeBuild projects, which then pass them as CDK context flags to `cdk deploy`.

**What gets configured by each value:**

| Config Key | What It Configures |
|---|---|
| `devUrl` | Smoke test target URL, dev Cognito callback URL, dev OIDC redirect URI |
| `prodDomainName` | Prod CloudFront alternate domain name, prod Cognito callback URL, prod OIDC redirect URI |
| `prodCertificateArn` | Prod CloudFront SSL certificate |

**DNS:** After the prod CloudFront distribution has the custom domain, create a CNAME or Route 53 alias record pointing `prodDomainName` to the CloudFront distribution domain.

---

## Pipeline Operations

### Monitoring Pipeline Runs

```bash
# List recent pipeline executions
aws codepipeline list-pipeline-executions \
  --pipeline-name OpenWebUI-Pipeline \
  --max-results 5 \
  --profile YOUR_PROFILE --region us-east-1

# Get current pipeline state
aws codepipeline get-pipeline-state \
  --name OpenWebUI-Pipeline \
  --profile YOUR_PROFILE --region us-east-1
```

### Approving or Rejecting a Deployment

Via the console: Go to CodePipeline → OpenWebUI-Pipeline → click **Review** on the approval action.

Via CLI:
```bash
# Approve
aws codepipeline put-approval-result \
  --pipeline-name OpenWebUI-Pipeline \
  --stage-name Test-and-Approve \
  --action-name Approve-Prod \
  --result "summary=Looks good,status=Approved" \
  --token TOKEN_FROM_EMAIL \
  --profile YOUR_PROFILE --region us-east-1

# Reject
aws codepipeline put-approval-result \
  --pipeline-name OpenWebUI-Pipeline \
  --stage-name Test-and-Approve \
  --action-name Approve-Prod \
  --result "summary=Found issues,status=Rejected" \
  --token TOKEN_FROM_EMAIL \
  --profile YOUR_PROFILE --region us-east-1
```

### Manually Triggering the Pipeline

```bash
aws codepipeline start-pipeline-execution \
  --name OpenWebUI-Pipeline \
  --profile YOUR_PROFILE --region us-east-1
```

### Viewing Build Logs

```bash
# Build stage logs
aws logs tail /aws/codebuild/OpenWebUI-Build --follow \
  --profile YOUR_PROFILE --region us-east-1

# Dev deploy logs
aws logs tail /aws/codebuild/OpenWebUI-Deploy-Dev --follow \
  --profile YOUR_PROFILE --region us-east-1

# Smoke test logs
aws logs tail /aws/codebuild/OpenWebUI-SmokeTest --follow \
  --profile YOUR_PROFILE --region us-east-1
```

### Viewing Application Logs

```bash
# Dev environment
aws logs tail /ecs/dev-open-webui --follow \
  --profile YOUR_PROFILE --region us-east-1

# Prod environment
aws logs tail /ecs/prod-open-webui --follow \
  --profile YOUR_PROFILE --region us-east-1

# Original manual deployment (unchanged)
aws logs tail /ecs/open-webui --follow \
  --profile YOUR_PROFILE --region us-east-1
```

---

## Relationship to Manual Deployment

The CI/CD pipeline is fully self-contained — it does not require a prior manual deployment via `deploy.sh`. Both paths use the same `DockerImageAsset` in `ComputeStack`, and both rely on the CDK bootstrap toolkit for the underlying container-assets ECR repo. The pipeline's deploy stages create all application infrastructure (VPC, database, ECS, etc.) from scratch.

If you happen to have an existing manual deployment, the pipeline does not touch it. They are completely independent stack sets.

### Coexistence (If Applicable)

If you previously deployed manually and now deploy the pipeline, you'll have independent stack sets:

| Stack Set | Deployed By | Stack Names |
|-----------|-------------|-------------|
| Manual (legacy) | `deploy.sh` | `OpenWebUI-Network`, `OpenWebUI-Data`, `OpenWebUI-Auth`, `OpenWebUI-Compute` |
| Dev | Pipeline Stage 3 | `OpenWebUI-Dev-Network`, `OpenWebUI-Dev-Data`, `OpenWebUI-Dev-Auth`, `OpenWebUI-Dev-Compute` |
| Prod | Pipeline Stage 5 | `OpenWebUI-Prod-Network`, `OpenWebUI-Prod-Data`, `OpenWebUI-Prod-Auth`, `OpenWebUI-Prod-Compute` |

The manual stacks are not modified, managed, or referenced by the pipeline. You can decommission them when ready.

### Backward Compatibility

The refactored CDK stacks remain fully backward compatible with `deploy.sh`:

- Running `cdk synth` without `-c environment=...` produces the original stack names with the original default values
- `deploy.sh` continues to work without any changes
- All new stack props are optional with defaults matching the original hardcoded values

---

## Migrating from Manual to Pipeline

If you want to transition your production workload from the manual deployment to the pipeline-managed prod environment:

### Step 1: Deploy the Pipeline and Let It Create Dev + Prod

Follow the [Deploy the Pipeline](#deploy-the-pipeline) steps above. The pipeline will create fresh dev and prod environments.

### Step 2: Configure Prod Environment

Set the prod domain and certificate in `infra/deploy.config.json`:

```json
{
  "prodDomainName": "oui.yourdomain.com",
  "prodCertificateArn": "arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID"
}
```

Alternatively, set them in `infra/lib/environment-config.ts` (committed to repo):

```typescript
export function getProdConfig(): EnvironmentConfig {
  return {
    // ... existing values ...
    domainName: 'oui.yourdomain.com',
    certificateArn: 'arn:aws:acm:us-east-1:ACCOUNT:certificate/CERT_ID',
  };
}
```

> **Note:** `deploy.config.json` is gitignored (account-specific values). `environment-config.ts` is committed. Use whichever fits your workflow — `deploy.config.json` values are read at pipeline stack deploy time and baked into the CodeBuild project as environment variables.

### Step 3: Migrate Users and Data

1. Export users from the original Cognito user pool
2. Import into the prod Cognito user pool
3. Migrate database data from the original Aurora cluster to the prod cluster (pg_dump/pg_restore)
4. Copy S3 uploads from the original bucket to the prod bucket

### Step 4: Switch DNS

Update your DNS CNAME to point to the prod CloudFront distribution domain.

### Step 5: Decommission the Manual Deployment

Once you've verified the pipeline-managed prod environment is working:

```bash
cd infra
npx cdk destroy OpenWebUI-Compute OpenWebUI-Auth OpenWebUI-Data OpenWebUI-Network \
  --profile YOUR_PROFILE
```

Note: Resources with `RemovalPolicy.RETAIN` (ECR repo, Aurora cluster, S3 bucket) will be preserved and must be deleted manually if desired.

---

## Configuration Reference

### Environment Config (`infra/lib/environment-config.ts`)

| Property | Dev | Prod | Default (no env) |
|----------|-----|------|-------------------|
| `environment` | `"dev"` | `"prod"` | N/A |
| `stackPrefix` | `"OpenWebUI-Dev"` | `"OpenWebUI-Prod"` | `"OpenWebUI"` |
| `auroraMinCapacity` | `0.5` | `0.5` | `0.5` |
| `auroraMaxCapacity` | `2` | `8` | `8` |
| `auroraDeletionProtection` | `false` | `true` | `true` |
| `ecsDesiredCount` | `1` | `1` | `1` |
| `ecsMinCapacity` | `1` | `1` | `1` |
| `ecsMaxCapacity` | `1` | `10` | `10` |
| `enableAutoScaling` | `false` | `true` | `true` |
| `domainName` | undefined | custom domain | from `deploy.config.json` (`prodDomainName`) |
| `certificateArn` | undefined | ACM cert ARN | from `deploy.config.json` (`prodCertificateArn`) |

### Pipeline Stack Props

| Prop | Required | Description |
|------|----------|-------------|
| `connectionArn` | Yes | CodeStar Connection ARN for GitHub |
| `repoOwner` | No | GitHub owner (default: `aws-samples`) |
| `repoName` | No | GitHub repo (default: `bedrock-open-webui`) |
| `branch` | No | Branch to trigger on (default: `main`) |
| `approvalEmail` | No | Email for approval notifications |
| `devUrl` | No | Dev CloudFront domain for smoke tests |
| `prodDomainName` | No | Custom domain for prod CloudFront distribution |
| `prodCertificateArn` | No | ACM certificate ARN for the prod custom domain |

### CDK Context Flags

| Flag | Purpose | Example |
|------|---------|---------|
| `-c environment=dev` | Synthesize/deploy dev stacks | `cdk synth -c environment=dev` |
| `-c environment=prod` | Synthesize/deploy prod stacks | `cdk synth -c environment=prod` |
| `-c pipeline=true` | Include the pipeline stack | `cdk deploy OpenWebUI-Pipeline -c pipeline=true` |
| (none) | Original stacks, backward compat | `cdk synth` |

---

## Troubleshooting

### Pipeline Fails at Source Stage

**Cause:** CodeStar Connection is not in `Available` status.

**Fix:** Go to Developer Tools → Connections in the AWS Console. If the connection shows `Pending`, click it and complete the GitHub authorization flow.

### Deploy Stage Fails — Docker Build or Asset Publish Error

**Cause:** CodeBuild can't run `docker build` inside `cdk deploy`, or the CDK asset publishing role can't push to the bootstrap-managed container-assets ECR repo.

**Fix:**
- Verify both deploy CodeBuild projects have `privileged: true` and `computeType` at least `MEDIUM` (the pipeline stack sets `LARGE`).
- Confirm the target account has been bootstrapped with a recent CDK bootstrap version: `aws cloudformation describe-stack-resource --stack-name CDKToolkit --logical-resource-id ImagePublishingRole`. If the resource isn't present, re-run `cdk bootstrap aws://<account>/<region>` with the latest CDK CLI.
- If the build itself fails, inspect the CodeBuild logs for the `docker build` step. Dependency issues (notably `npm ci` with a stale lockfile) surface here — see the next entry.

### Deploy Stage Fails — Docker Build Error

**Cause:** Usually a `package-lock.json` mismatch or dependency issue.

**Fix:** Regenerate the lockfile:
```bash
docker run --rm -v $(pwd):/app -w /app node:22-alpine3.20 \
  sh -c "rm -rf node_modules package-lock.json && npm install --force"
sudo rm -rf node_modules
git add package-lock.json && git commit -m "fix(build): regenerate lockfile" && git push origin main
```

### Deploy Stage Fails — CDK Bootstrap Missing

**Cause:** CDK hasn't been bootstrapped in the target account/region.

**Fix:**
```bash
cd infra
npx cdk bootstrap aws://ACCOUNT_ID/REGION --profile YOUR_PROFILE
```

### Deploy Stage Fails — CloudFormation Stack Rollback

**Cause:** A resource creation failed (e.g., Cognito domain prefix already taken, security group limit reached).

**Fix:** Check the CloudFormation console for the specific stack that failed. Look at the Events tab for the failure reason. Common issues:
- Cognito domain prefix collision → The prefix includes the environment name and account ID, so this is rare. If it happens, check for leftover stacks from a previous failed deployment.
- Resource limits → Request a limit increase via Service Quotas.

### Smoke Tests Fail

**Cause:** The dev ECS task hasn't stabilized within 60 seconds, or the application has a startup error.

**Fix:**
1. Check dev application logs: `aws logs tail /ecs/dev-open-webui --since 5m`
2. Check ECS task status in the console (OpenWebUI-Dev cluster)
3. Verify the `DEV_URL` environment variable in the smoke test CodeBuild project matches the actual dev CloudFront domain

### Approval Email Not Received

**Cause:** SNS subscription not confirmed.

**Fix:** Check your email (including spam) for the SNS subscription confirmation. If you don't see it:
```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:ACCOUNT:OpenWebUI-Pipeline-Approval \
  --profile YOUR_PROFILE --region us-east-1
```

If the subscription shows `PendingConfirmation`, the confirmation email was sent but not clicked. You can also approve directly in the CodePipeline console without email.

### Pipeline Stuck at Approval

**Cause:** No one has approved or rejected the deployment.

**Fix:** Approve or reject via the CodePipeline console, or use the CLI commands in the [Pipeline Operations](#pipeline-operations) section. Approvals time out after 7 days by default.

---

## Files Reference

| File | Purpose |
|------|---------|
| `infra/lib/environment-config.ts` | Dev/prod configuration interface and factory functions |
| `infra/lib/pipeline-stack.ts` | CodePipeline, CodeBuild projects, SNS topic, IAM roles |
| `infra/bin/app.ts` | CDK app entry — reads environment context, prefixes stacks, gates pipeline |
| `buildspec-smoke.yml` | Smoke test stage — health and API endpoint checks |
| `infra/lib/data-stack.ts` | Aurora capacity and deletion protection (parameterized) |
| `infra/lib/auth-stack.ts` | Cognito domain prefix (parameterized) |
| `infra/lib/compute-stack.ts` | ECS scaling, auto-scaling toggle, resource name prefixing |

---

## Related Resources

- [AWS Deployment Guide](AWS_DEPLOYMENT_GUIDE.md) — Initial manual deployment
- [AWS CodePipeline User Guide](https://docs.aws.amazon.com/codepipeline/latest/userguide/)
- [AWS CodeBuild User Guide](https://docs.aws.amazon.com/codebuild/latest/userguide/)
- [AWS CDK Developer Guide](https://docs.aws.amazon.com/cdk/v2/guide/)
- [CodeStar Connections](https://docs.aws.amazon.com/dtconsole/latest/userguide/connections.html)
