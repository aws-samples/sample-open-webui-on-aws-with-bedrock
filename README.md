# Deploy Open WebUI on AWS with native Amazon Bedrock support

Deploy [Open WebUI](https://github.com/open-webui/open-webui) on AWS (Amazon ECS
on Fargate) with a native Amazon Bedrock provider — an AWS deployment sample.

> **About the application.** This is a deployment sample for the third-party
> Open WebUI project by Open WebUI Inc. Open WebUI is **not included in this
> repository**: it is obtained at build/deploy time from its official
> distribution channels and is licensed separately under the Open WebUI License
> (see [`NOTICE`](NOTICE) and [`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md)).
> The sample deploys it with **unmodified branding**. The application is the
> official Open WebUI release (pinned to **v0.10.2**), unmodified except for a
> small, clearly-attributed Bedrock provider addition: **2 new backend modules +
> 5 patches totaling ≈85 lines** (an optional admin-panel UI adds 1 module + 2
> patches). Everything AWS-authored is under [`overlay/`](overlay/),
> [`patches/`](patches/), [`infra/`](infra/), [`scripts/`](scripts/) and
> [`docs/`](docs/).

The pin is **v0.10.2** because that upstream release includes security and
access-control fixes (upstream advises production deployments to update);
[`docs/UPGRADE_RUNBOOK.md`](docs/UPGRADE_RUNBOOK.md) covers bumping the pin.

## Architecture

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

Request flow: CloudFront (WebSocket-capable VPC origin) → internal ALB → ECS
Fargate → Aurora Serverless v2 / ElastiCache Redis / S3, with Cognito OIDC for
sign-in, Secrets Manager for all secrets, and the Bedrock Converse API for
model inference.

## What you get

- **Native Bedrock provider** — Converse/ConverseStream streaming, tool
  calling, inference-profile discovery, and per-response token usage emitted in
  OpenAI-shape fields so Open WebUI's native usage display works unchanged.
- **Model access control** — Open WebUI's native per-model access grants,
  automated by [`scripts/set-model-access.sh`](scripts/set-model-access.sh);
  coarse allow-listing via `BEDROCK_ALLOWED_MODELS`.
- **Cognito SSO** — configuration-only integration through Open WebUI's
  built-in OIDC support (no auth code changes).
- **Three deploy modes** — one-command (`./deploy.sh`), single pipeline, or
  multi-environment CI/CD (see [`docs/CICD_DEPLOYMENT_GUIDE.md`](docs/CICD_DEPLOYMENT_GUIDE.md)).
- **Two image targets** — `backend` (default: the official image + Bedrock
  provider only) and `full` (opt-in: rebuilds the UI with an admin Connections
  panel section for Bedrock settings).

## What you do NOT get (by design)

No token metering, no per-user quota enforcement, no cost dashboards — out of
scope for this sample. For cost management, use AWS-native mechanisms: Bedrock
service quotas, CloudWatch Bedrock usage metrics, application inference
profiles for cost allocation, and AWS Budgets. See
[`docs/COST_ANALYSIS_20K_USERS.md`](docs/COST_ANALYSIS_20K_USERS.md) for
sizing and cost-control strategies.

## Prerequisites

- An AWS account with permission to create the resources above
- AWS CLI v2, configured (`aws configure` / SSO)
- Node.js 18–22 and npm
- Docker (CDK builds the container image locally during deploy)
- Amazon Bedrock model access enabled in your target region
  ([console → Bedrock → Model access](https://console.aws.amazon.com/bedrock/home#/modelaccess))
- Optional: a custom domain + ACM certificate in us-east-1

## Deploy

```bash
git clone https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock.git
cd sample-open-webui-on-aws-with-bedrock
cp .env.example .env       # review/edit application settings
./deploy.sh                # guided deployment
```

Full instructions, including CI/CD pipelines and multi-environment setups:
[`docs/AWS_DEPLOYMENT_GUIDE.md`](docs/AWS_DEPLOYMENT_GUIDE.md).

## Configuration

Application settings live in `.env` (from [`.env.example`](.env.example)). The
Bedrock provider reads four variables:

| Variable | Purpose | Default |
|---|---|---|
| `ENABLE_BEDROCK_API` | Enable the provider | `false` (infra sets `true`) |
| `BEDROCK_REGION` | Bedrock region | `us-east-1` (infra sets the deploy region) |
| `BEDROCK_ENDPOINT_URL` | Custom endpoint (VPC endpoint) | empty |
| `BEDROCK_ALLOWED_MODELS` | Comma-separated model allow-list | empty (all visible models) |

All four are also admin-editable at runtime via
`GET/POST /api/v1/bedrock/config` and `/api/v1/bedrock/config/update` (and, in
the `full` image target, in Admin Settings → Connections). See
[`docs/BEDROCK_INTEGRATION_GUIDE.md`](docs/BEDROCK_INTEGRATION_GUIDE.md).

## Cost

Sample cost breakdown with default parameters in `us-east-1` for one month
(prices as of mid-2026 — verify with the AWS Pricing Calculator). Bedrock token
usage is excluded (varies with use).

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

Aurora scales to 0.5 ACU when idle; ECS auto-scales up to 10 tasks under load.
Consider an [AWS Budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html)
to monitor spending.

## Security notes

- Private networking: the ALB is internal; only CloudFront reaches it through a
  VPC origin (ingress restricted to the CloudFront origin-facing prefix list).
- All secrets (app secret key, Cognito client secret, DB password) live in
  Secrets Manager and are injected at task launch.
- The Bedrock IAM policy is narrow (Converse/model-listing) and attached only
  when Bedrock is enabled.
- WAF can be added on the CloudFront distribution as an extension.

## Known limitations

- Upstream is pinned to Open WebUI v0.10.2; bumping the pin requires
  re-validating the 7 patches ([`docs/UPGRADE_RUNBOOK.md`](docs/UPGRADE_RUNBOOK.md)).
- The admin Bedrock Connections panel exists only in the opt-in `full` image
  target; the default `backend` target configures Bedrock via env vars or the
  REST admin API.
- No per-user quota enforcement (see "What you do NOT get").

## Uninstall

```bash
cd infra
npx cdk destroy --all
```

Resources with retention policies (S3 upload bucket, Aurora final snapshot,
CloudWatch logs) must be deleted manually afterwards — see the
[deployment guide](docs/AWS_DEPLOYMENT_GUIDE.md) uninstall section.

## Contributing / Security / License

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [Security issue notifications](SECURITY.md)
- License: [MIT-0](LICENSE) for AWS-authored content; Open WebUI is licensed
  separately (see [NOTICE](NOTICE) and [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)).
