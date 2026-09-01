# Deploy and operate the sample on AWS

[Documentation home](README.md) · [Architecture](GATEWAY_INTEGRATION_GUIDE.md) ·
[Metering](METERING.md) · [Upgrades](UPGRADE_RUNBOOK.md) · [Costs](COSTS.md)

This guide is the supported consumer path for configuring, deploying,
validating, operating, and removing the sample. It assumes a test or evaluation
environment.

> [!CAUTION]
> This repository is sample code, has not been through an application security
> review, and is not suitable for production use as-is. You own the deployed
> third-party application, security review, threat model, hardening, scale
> testing, data lifecycle, operations, and cost. See [`DISCLAIMER.txt`](../DISCLAIMER.txt).

## Before you deploy

### Architecture at a glance

![Architecture flow from a user and Cognito through CloudFront, private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and optional metering services.](images/architecture-light.svg#gh-light-mode-only)
![Architecture flow from a user and Cognito through CloudFront, private Open WebUI on Fargate, an AgentCore gateway, Amazon Bedrock, and optional metering services.](images/architecture-dark.svg#gh-dark-mode-only)

The [gateway integration guide](GATEWAY_INTEGRATION_GUIDE.md) owns the diagram,
identity boundary, and lane mechanics. Important deployment facts:

- the repository runs an unmodified official Open WebUI image and builds no
  application image;
- Cognito authenticates the user at Open WebUI and AgentCore validates the
  user's JWT, while the gateway IAM role invokes Mantle;
- CloudFront viewer traffic is HTTPS, but the VPC-origin and ALB-to-task hops
  are HTTP;
- the ALB, Fargate tasks, Aurora, and Redis are private; NAT and managed-service
  egress still exist; and
- metering is optional and off by default.

### Prerequisites

Install or obtain:

- an AWS account and a least-privilege deployment role authorized for the
  resources in the stack;
- AWS CLI v2 with a working profile/session;
- Node.js 20 or newer and npm;
- Python 3 and pip; and
- Git.

Docker is not required. `deploy.sh` installs infrastructure dependencies,
vendors the Python SDK packages needed by Lambda assets, and resolves the
upstream container reference.

Confirm credentials before deployment:

```bash
aws sts get-caller-identity --profile YOUR_PROFILE
```

Treat an unknown account or role as a stop condition.

### Region and model prerequisites

The script defaults to `us-east-1`, the documented region for the full
three-lane experience. Before choosing another region, verify current support
for AgentCore gateways/inference, the Mantle APIs, CloudFront VPC origins,
Aurora PostgreSQL Serverless v2, ElastiCache, and every other required service.
Model availability and account authorization are separate from service
availability.

The committed [`config/model-capabilities.json`](../config/model-capabilities.json)
is a dated us-east-1 probe snapshot. It is not regenerated during a normal
deploy. Probe the intended account/region before presenting the resulting model
menu as validated:

```bash
aws sts get-caller-identity --profile YOUR_PROFILE
python3 scripts/probe-model-capabilities.py \
  --profile YOUR_PROFILE \
  --region us-east-1 \
  --out config/model-capabilities.json \
  --yes

git diff -- config/model-capabilities.json
```

Review and commit intentional changes before deployment. See
[Operating the model catalog](GATEWAY_INTEGRATION_GUIDE.md#operating-the-model-catalog).

### Cost and data-retention review

Build a workload estimate with [`COSTS.md`](COSTS.md). Do not deploy from a
static monthly number. In particular, account for two NAT gateways, Aurora
writer/reader capacity, Redis, ALB/CloudFront, endpoints, logs, model usage, and
optional metering resources.

Aurora, the Cognito user pool, and the upload bucket use retention-oriented
behavior. Stack deletion does not necessarily remove data or stop every charge.
Decide backup, restore, retention, and cleanup ownership before creating data.

## Configure

### 1. Clone and create local state

```bash
git clone https://github.com/aws-samples/sample-open-webui-on-aws-with-bedrock.git
cd sample-open-webui-on-aws-with-bedrock
cp .env.example .env
```

`.env` is ignored deployment/application state. Never commit it. Secrets are
stored in Secrets Manager rather than in `.env`.

### 2. Select the upstream image behavior

Leave `OPEN_WEBUI_IMAGE` unset to have each full deploy discover the latest
official release and normally resolve it to an immutable multi-architecture
digest. To control upgrades, set a validated release tag or digest:

```bash
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:vX.Y.Z
# or
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui@sha256:YOUR_RECORDED_DIGEST
```

Do not use `:latest`; upstream uses it for its main-branch build rather than a
release. A tag can remain unresolved if registry access fails, and a custom
registry override has different provenance. Read the
[upgrade runbook](UPGRADE_RUNBOOK.md) before pinning or changing a version.

### 3. Decide optional features

- **Consumption governance:** add `--metering` to every full deploy that should
  wire the module.
- **Scheduled model refresh:** set `ENABLE_MODEL_REFRESH=true` in `.env` and,
  optionally, `MODEL_REFRESH_RATE_HOURS=24`.
- **Custom domain:** pass `--domain` and an ACM certificate ARN from
  `us-east-1` with `--cert-arn`. DNS remains your responsibility.
- **Task sizing:** use `--cpu` and `--memory` or their `.env` values.

For metering OBSERVE mode, add `--metering-mode observe`. Older metering
installations that need the first staged GSI pass use
`--metering-gsi-phase 1`; follow the metering guide and remove that flag on the
second pass.

### 4. Review self-registration and groups

The Cognito pool enables email self-sign-up by default and creates `admin`,
`user`, `power-users`, and `basic-users` groups. The application permits the
configured role groups. Decide whether open self-registration is acceptable in
your test environment and change it before exposing the URL if it is not.

For reliable initial administration, register/create the operator and add that
identity to `admin` before its first application session. Do not rely on
upstream first-user promotion as the role-management plan.

## Deploy

### Interactive

```bash
./deploy.sh
```

### Non-interactive

```bash
./deploy.sh \
  --profile YOUR_PROFILE \
  --region us-east-1 \
  --yes
```

For the opt-in module:

```bash
./deploy.sh --metering \
  --profile YOUR_PROFILE \
  --region us-east-1 \
  --yes
```

Use `--skip-bootstrap` only after the account/region is bootstrapped. Use
`--env-only` only after a full deployment exists; it updates task environment
state and restarts the ECS service without synthesizing/deploying CDK.

### What the script does

The supported script:

1. validates local tools, AWS credentials, account, and configuration;
2. installs CDK dependencies and optionally builds the metering console;
3. vendors current SDK dependencies required by provisioner/refresher assets;
4. bootstraps the target account/region unless skipped;
5. resolves the selected Open WebUI image reference;
6. runs `cdk deploy --all` with the selected feature/sizing contexts;
7. reads stack outputs and writes non-secret local `.env` state;
8. synchronizes the Cognito app-client secret into Secrets Manager;
9. reconciles final Cognito callback/logout URLs; and
10. registers/updates the ECS task definition with application environment
    values and waits for the service deployment.

A bare `npx cdk deploy --all` does not reproduce all of those steps and is a
maintainer primitive, not an equivalent consumer deployment path.

### Stack graph

The base application has five stacks; metering adds a sixth:

| Stack | Depends on | Purpose |
|---|---|---|
| `OpenWebUI-Network` | — | VPC, subnets, NAT, endpoints, security groups |
| `OpenWebUI-Data` | Network | Aurora, Redis, upload bucket |
| `OpenWebUI-Auth` | — | Cognito user pool/client/domain/groups |
| `OpenWebUI-Gateway` | Auth | AgentCore gateway, interceptor, target, optional model refresh |
| `OpenWebUI-Metering` | Gateway | Optional table, settlement/recovery/pricing/assurance, API, console |
| `OpenWebUI-Compute` | Data, Auth, Gateway, and Metering when enabled; also consumes Network resources | Fargate, internal ALB, CloudFront, application secrets/runtime wiring |

CDK can deploy independent branches concurrently. Do not describe this graph as
one fixed linear stack order.

## Validate

### 1. Confirm stack state and outputs

```bash
aws cloudformation describe-stacks \
  --stack-name OpenWebUI-Compute \
  --profile YOUR_PROFILE --region us-east-1 \
  --query "Stacks[0].{Status:StackStatus,AppUrl:Outputs[?OutputKey=='AppUrl']|[0].OutputValue,Image:Outputs[?OutputKey=='AppImageUri']|[0].OutputValue}"
```

Expect a complete status, an HTTPS application URL, and the selected image
reference. With metering enabled, also inspect:

```bash
aws cloudformation describe-stacks \
  --stack-name OpenWebUI-Metering \
  --profile YOUR_PROFILE --region us-east-1 \
  --query "Stacks[0].{Status:StackStatus,Console:Outputs[?OutputKey=='ConsoleUrl']|[0].OutputValue,Api:Outputs[?OutputKey=='AdminApiUrl']|[0].OutputValue}"
```

Do not publish these deployment-specific URLs or identifiers in screenshots.

### 2. Establish an administrator

The pool supports self-registration. After the operator exists, add it to the
`admin` Cognito group in the console or with the CLI:

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id YOUR_USER_POOL_ID \
  --username operator@example.com \
  --group-name admin \
  --profile YOUR_PROFILE --region us-east-1
```

Sign out and back in after changing groups so the new access token carries the
claim.

### 3. Confirm runtime seeding

The seeder waits for Open WebUI's schema and runs in the background during task
startup. It does not wait for an admin login. Check recent application logs:

```bash
aws logs tail /ecs/open-webui \
  --since 15m \
  --profile YOUR_PROFILE --region us-east-1
```

Confirm it installed/refreshed the Claude pipe and native `gw`/`gwr`
connections. An application health check alone does not prove the integrations
seeded.

### 4. Walk all three lanes

In the browser:

1. complete Cognito sign-in;
2. confirm Chat Completions, Responses, and Claude entries appear as expected
   for the current region/account;
3. send one non-sensitive test prompt through a model in each non-empty lane;
4. verify streaming and final usage presentation where available; and
5. inspect gateway/interceptor and ECS logs for errors.

A populated model dropdown is not sufficient proof of invocation. Conversely,
a legitimately empty regional Claude catalog is not necessarily a pipe defect.

### 5. Validate metering separately

If enabled, follow [`METERING.md#enable-and-validate`](METERING.md#enable-and-validate).
In particular, run the first pricing refresh before relying on USD limits and
understand that the capture canary begins at EventBridge rather than at the Open
WebUI filter.

## Post-deployment configuration

### Model access control

Cognito groups synchronize to Open WebUI groups on login. Use stock Open WebUI
model visibility under **Workspace → Models** to decide which groups can see
which models. Group membership is managed in Cognito; model visibility is
managed in Open WebUI.

The gateway capability filter answers “which API lane can advertise this model
according to the snapshot.” It is not authorization policy and does not replace
Open WebUI visibility controls.

### Custom domain and DNS

The stack can attach a custom domain/certificate to CloudFront, but it does not
create Route 53 records. Create the appropriate DNS record after deployment and
re-run the full deploy if callback/logout values need reconciliation.

### Additional providers

Open WebUI can be configured for other OpenAI-compatible endpoints through
`.env`, but those providers are outside this sample's AgentCore identity,
capability, metering, and pricing contract. Do not imply the Bedrock governance
module automatically covers them.

## Security boundaries

### Network

- CloudFront is the public entry point and redirects viewers to HTTPS.
- The ALB is internal and ingress is limited to the CloudFront origin-facing
  managed prefix list.
- CloudFront's VPC-origin policy is HTTP-only; the ALB listener is HTTP/80 and
  targets the task on HTTP/8080.
- Fargate, Aurora, and Redis use private-with-egress subnets.
- NAT gateways and selected VPC endpoints provide egress. AgentCore/Mantle and
  GHCR interactions are not proven to stay inside the VPC.
- CloudFront caching is disabled for the application behavior.

If end-to-end TLS or no-internet-egress is a requirement, change and review the
architecture rather than describing the default as compliant.

### Identity and permissions

- Cognito Managed Login and authorization code flow provide user sessions.
- AgentCore validates the user's access token; the gateway IAM role invokes
  Mantle.
- The gateway and project-provisioning roles currently require broad
  `bedrock-mantle:*` permissions on `*` in this implementation.
- The Fargate task role has scoped bucket/secret access plus Mantle discovery
  permissions and log permissions.
- AgentCore Policy and Bedrock Guardrails are not deployed by default.
- Cognito self-sign-up is enabled; decide whether to keep it.

### Data protection and retention

Aurora, Redis, and S3 use encryption-at-rest settings in the stacks; Redis uses
TLS. Do not claim all connections are TLS: the documented CloudFront/ALB/task
hops are HTTP, and this stack does not explicitly add an Aurora SSL mode to the
application database URL.

Application secrets are generated/stored in Secrets Manager and injected into
the task. `.env` contains deployment/application state and must remain local.

### Production considerations

Before any use outside a disposable evaluation environment:

- run a security review and threat model covering upstream Open WebUI, Cognito,
  gateway JWT handling, IAM, network transport, admin API/console, data stores,
  logs, and deployment-script mutations;
- pin and test an Open WebUI digest you own operationally;
- disable or govern self-registration;
- narrow permissions where the current service authorization surface permits;
- define backups, restore tests, retention, deletion, and privacy handling;
- test Bedrock throttling, gateway/interceptor failure, database failover,
  Redis loss/eviction, task scale-out, WebSocket behavior, and metering gaps;
- configure model safety controls appropriate to the use case;
- add dependency/container scanning and patch ownership; and
- establish alerts, runbooks, incident response, and cost ownership.

The sample's alarms and defaults are starting evidence, not certification.

## Operations

### Application and Lambda logs

```bash
aws logs tail /ecs/open-webui --follow \
  --profile YOUR_PROFILE --region us-east-1
```

Use the Lambda log groups for gateway listing/admission, target provisioning,
pricing, settlement, canaries, and admin API failures. Avoid copying access
tokens or sensitive chat content into tickets or public issues.

### Service health

Check:

- ECS service deployments, task health, and desired/running counts;
- ALB target health and the no-healthy-host alarm;
- Aurora and Redis state;
- CloudFront distribution status;
- AgentCore gateway and target state; and
- metering DLQ, canaries, pricing freshness, and alarms when enabled.

### Configuration-only update

```bash
./deploy.sh --env-only --profile YOUR_PROFILE --region us-east-1
```

This restarts the service but does not deploy CDK. It is not appropriate for an
image, gateway, stack, or metering-topology change.

### Full update

```bash
./deploy.sh --skip-bootstrap --profile YOUR_PROFILE --region us-east-1
# retain --metering when the deployment uses it
```

With `OPEN_WEBUI_IMAGE` unset, every full deploy re-resolves the latest official
release. Follow the upgrade runbook and record the resulting digest.

## Troubleshooting

### Sign-in redirects or reports `redirect_mismatch`

Run the full deploy again and compare the final application URL with the
Cognito app client's callback/logout URLs. A custom-domain change requires
both CloudFront/DNS and Cognito reconciliation.

### User signs in but is not an administrator

Add the user to `admin`, sign out, and sign in again so the group claim is
refreshed. Do not depend on account creation order.

### No models appear

1. Check the seeder output in `/ecs/open-webui` logs.
2. Confirm `gw`, `gwr`, and the global Claude pipe are present.
3. Confirm the capability snapshot matches the target region/account.
4. Check gateway interceptor logs for model-list flavor/path handling.
5. For Claude, check direct catalog discovery, task-role permissions, and
   regional `anthropic.*` availability.

### A listed model fails

A snapshot can be stale or a model can be unavailable/account-gated despite
listing. Run the probe with deployment credentials, capture the exact service
response, and update/redeploy the snapshot or refresh the target. Do not solve
it by advertising every catalog model in every lane.

### Claude asks for SSO

The pipe did not find a usable OAuth session and `SIGV4_FALLBACK` is off. Use
Cognito sign-in. Enabling fallback switches to a shared Fargate task-role call
and loses per-user gateway attribution.

### Application returns 5xx/504

Check ECS deployment stability, running tasks, ALB target health, and the
application logs before invalidating or changing CloudFront. The application
cache policy is already disabled; a stale-cache explanation is usually wrong.

### Database connection fails at startup

Confirm the ECS task definition has the database host/port/name/user values and
Secrets Manager database-password reference. Re-run the supported deploy path
to reconcile task state rather than hand-editing a running task definition.

### Metering denies or records unexpected values

Read the [enforcement contract](METERING.md#enforcement-contract), then inspect
policy precedence, current counter/reservations, pricing coverage, degraded
signals, and the ledger. Group policies are advisory and unpriced calls settle
at $0.

## Cleanup

> [!WARNING]
> Cleanup is destructive and can still leave retained, billable resources. Run
> it only in the intended account/region after reviewing CloudFormation change
> scope, backups, and retained data. Never use placeholders without confirming
> `aws sts get-caller-identity`.

Set and verify the exact profile/region, then inventory the named stacks before
running CDK:

```bash
PROFILE=YOUR_PROFILE
REGION=YOUR_DEPLOYMENT_REGION
ACCOUNT=$(aws sts get-caller-identity \
  --profile "$PROFILE" --query Account --output text)

printf 'Account: %s\nRegion:  %s\n' "$ACCOUNT" "$REGION"
aws cloudformation describe-stacks \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Stacks[?starts_with(StackName, `OpenWebUI-`)].[StackName,StackStatus]' \
  --output table
```

Stop unless the account, region, and stack list exactly match the deployment you
intend to remove. From the repository root, prepare a clean clone for synthesis:

```bash
npm --prefix infra ci
# Required when the stack list includes OpenWebUI-Metering:
npm --prefix console ci
npm --prefix console run build
```

Destroy the base deployment with an explicitly bound CDK environment:

```bash
cd infra
CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION" AWS_REGION="$REGION" \
  npx cdk destroy \
    OpenWebUI-Compute OpenWebUI-Gateway OpenWebUI-Auth \
    OpenWebUI-Data OpenWebUI-Network \
    --profile "$PROFILE"
```

For a metering deployment, include the Metering stack and context:

```bash
cd infra
CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION" AWS_REGION="$REGION" \
  npx cdk destroy \
    OpenWebUI-Compute OpenWebUI-Metering OpenWebUI-Gateway \
    OpenWebUI-Auth OpenWebUI-Data OpenWebUI-Network \
    --context metering=on \
    --profile "$PROFILE"
```

Read the region-bound change prompt and confirm every named stack; do not use
forced/noninteractive deletion in an environment you have not independently
verified.

After stack removal, inventory and deliberately handle:

- retained Aurora clusters/snapshots and deletion protection;
- retained Cognito user pools/users;
- retained or non-empty S3 buckets and object versions;
- Secrets Manager secrets and recovery windows;
- CloudWatch log groups, alarms, dashboards, and SNS subscriptions;
- any archived Mantle Projects;
- metering table/backups if a stack could not be deleted cleanly; and
- NAT, endpoints, ENIs, or network resources blocked by retained dependencies.

Do not disable deletion protection or remove retained data merely to make a
cleanup command green. Decide and document the data outcome first.

## Related guidance

- [Gateway architecture and model lanes](GATEWAY_INTEGRATION_GUIDE.md)
- [Metering contract and operations](METERING.md)
- [Open WebUI upgrades and rollback](UPGRADE_RUNBOOK.md)
- [Cost planning](COSTS.md)
- [Infrastructure maintainer notes](../infra/README.md)
