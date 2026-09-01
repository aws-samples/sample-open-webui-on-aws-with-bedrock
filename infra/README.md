# AWS CDK infrastructure

[Documentation home](../docs/README.md) · [Deployment guide](../docs/AWS_DEPLOYMENT_GUIDE.md) ·
[Gateway architecture](../docs/GATEWAY_INTEGRATION_GUIDE.md)

This directory contains the TypeScript AWS CDK v2 application. Consumers should
deploy from the repository root with `./deploy.sh`; bare CDK does not perform
image resolution, dependency vendoring, output collection, Cognito secret and
callback reconciliation, or the final ECS environment update.

## Composition

[`bin/app.ts`](bin/app.ts) is the composition root. Domain resources remain in
the corresponding stack files under [`lib/`](lib/).

| Stack | Primary resources | Dependency |
|---|---|---|
| `OpenWebUI-Network` | VPC, two-AZ public/private subnets, NAT gateways, endpoints, security groups | — |
| `OpenWebUI-Data` | Aurora PostgreSQL Serverless v2/pgvector, ElastiCache Redis, S3 uploads | Network |
| `OpenWebUI-Auth` | Cognito user pool, app client, Managed Login, role groups | — |
| `OpenWebUI-Gateway` | AgentCore gateway, interceptor, target custom resource, optional capability refresher | Auth |
| `OpenWebUI-Metering` | Optional table, settlement/recovery/pricing/assurance, admin API, console | Gateway |
| `OpenWebUI-Compute` | Fargate, internal ALB, CloudFront VPC origin, application secrets and runtime assets | Data, Auth, Gateway, Metering when enabled; consumes Network resources |

CDK may deploy independent branches concurrently; this is not one fixed linear
stack order.

## Consumer deployment

From the repository root:

```bash
cp .env.example .env
./deploy.sh --profile YOUR_PROFILE --region us-east-1
# add --metering only when the optional module is intended
```

See the [deployment guide](../docs/AWS_DEPLOYMENT_GUIDE.md). Do not present
`npx cdk deploy --all` as an equivalent quick start.

## Configuration precedence

`bin/app.ts` resolves most configuration in this order:

1. CDK CLI context;
2. ignored local `infra/deploy.config.json`; and
3. code/environment preset defaults.

`deploy.sh` forwards its supported flags and selected `.env` values into this
contract. Not every CDK context key has a shell flag.

| Context | Default | Maintainer meaning |
|---|---|---|
| `openWebuiImage` | fallback tag in `lib/compute-stack.ts` | Consumer script normally supplies a resolved tag/digest. Bare CDK uses the fallback. |
| `metering` | `off` | `on` creates Metering and switches Gateway/Compute integration. Consumer flag: `--metering`. |
| `meteringMode` | `enforce` | `observe` logs healthy deny decisions but still writes admission state. Consumer flag: `--metering-mode`. |
| `meteringGsiPhase` | unset | One-time staged GSI rollout for older metering tables. Consumer flag: `--metering-gsi-phase 1`. |
| `enableModelRefresh` | `false` | Adds scheduled probe, collapse guard, target refresh, and SNS diff. Consumer `.env`: `ENABLE_MODEL_REFRESH=true`. |
| `modelRefreshRateHours` | `24` | Refresher cadence when enabled. |
| `domainName` / `certificateArn` | unset | Custom CloudFront domain/certificate; consumer flags persist them locally. |
| `fargateCpu` / `fargateMemory` | 1024 / 2048 | Task sizing; consumer flags: `--cpu`, `--memory`. |
| `environment` | unset | Maintainer dev/prod presets with prefixed stack names. `deploy.sh` expects default unprefixed outputs and does not support this context end to end. |

Do not commit `deploy.config.json` or `cdk.context.json`; they are local
deployment state.

## Image and runtime integration

Compute uses `ContainerImage.fromRegistry` and never builds Open WebUI. The
supported script selects an official release by default and attempts to resolve
it to a digest. The Fargate command downloads AWS-authored pipe/seeder assets,
runs the seeders in the background, then executes upstream `bash start.sh`.

The exact guarantees differ for a custom registry, an unresolved tag fallback,
or bare CDK. See the [upgrade runbook](../docs/UPGRADE_RUNBOOK.md).

## Important implementation boundaries

- CloudFront redirects viewers to HTTPS but uses an HTTP-only VPC origin to the
  internal ALB; the ALB forwards HTTP to port 8080.
- CloudFront caching is disabled for the application behavior.
- `VECTOR_DB=pgvector` keeps retrieval vectors in Aurora rather than ephemeral
  task storage.
- Redis TLS and the Redis WebSocket manager share Socket.IO state across tasks.
- Cognito group claims drive Open WebUI role/group synchronization.
- The gateway validates the user's JWT; its execution role invokes Mantle.
- Metering wiring is conditional. With `metering=off`, no Metering stack or
  metering runtime assets/environment are synthesized.

## Validation

From `infra/`:

```bash
npm install
npm run build
npx tsc --noEmit
npx cdk synth --quiet
```

Use `npx cdk diff` with an explicitly verified account/profile before a real
infrastructure change. CDK synthesis can require the same local generated
console artifacts or vendored dependencies that `deploy.sh` prepares.

For repository-wide checks, also run:

```bash
cd ..
python3 -m pytest metering/tests -q
node scripts/docs-integrity.mjs
node scripts/docs-check.mjs
bash -n deploy.sh
```

## Related source

- [`lib/gateway-stack.ts`](lib/gateway-stack.ts) — gateway/interceptor/target/refresher
- [`lib/metering-stack.ts`](lib/metering-stack.ts) — optional governance control plane
- [`lib/metering-console.ts`](lib/metering-console.ts) — console distribution and PKCE client
- [`lib/compute-stack.ts`](lib/compute-stack.ts) — upstream image, runtime assets, Fargate/ALB/CloudFront
- [`bin/app.ts`](bin/app.ts) — cross-stack composition and dependencies
