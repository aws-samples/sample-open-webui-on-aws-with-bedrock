# Open WebUI upgrade and rollback runbook

[Documentation home](README.md) · [Deployment guide](AWS_DEPLOYMENT_GUIDE.md) ·
[Gateway integration](GATEWAY_INTEGRATION_GUIDE.md)

This repository vendors no Open WebUI source and builds no application image.
A supported deployment runs an official `ghcr.io/open-webui/open-webui` release
and installs the AWS integration at runtime. An upgrade therefore changes the
selected image while preserving and revalidating the database and integration
contracts.

> [!CAUTION]
> Open WebUI is a separately licensed third-party application. Review its
> release notes and license, test the target release in an isolated environment,
> and own the database migration/rollback outcome. This sample does not certify
> an upstream release.

## Image-selection contract

`OPEN_WEBUI_IMAGE` lives in the ignored root `.env` file:

| Selection | Supported deploy behavior |
|---|---|
| Unset (default) | `deploy.sh` discovers the latest official release tag at deploy time and attempts to resolve its multi-architecture GHCR index to a digest. A later full deploy can therefore select a newer release. |
| Official release tag | The resolver attempts to replace that tag with its immutable digest. If registry access fails, the script warns and passes the floating tag through. |
| Official digest | The exact reference is used without registry resolution. This is the most reproducible selection and the rollback format to record. |
| Custom registry/reference | Passed through; the repository cannot claim official provenance or immutability for it. |

Do not set `:latest`; upstream uses it for a main-branch image rather than a
release. Bare CDK also has a fallback tag and skips important deployment-script
steps, so use `./deploy.sh` for the lifecycle described here.

## 1. Record the running state

Confirm the account and region first:

```bash
aws sts get-caller-identity --profile YOUR_PROFILE
```

Record the image reference CloudFormation knows:

```bash
aws cloudformation describe-stacks \
  --stack-name OpenWebUI-Compute \
  --profile YOUR_PROFILE --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='AppImageUri'].OutputValue" \
  --output text
```

Also record:

- current task-definition revision and each running task's `imageDigest`;
- current application URL;
- whether metering and scheduled model refresh are enabled;
- Aurora cluster identifier and latest restorable time;
- current stack status and any active alarms; and
- the exact `.env` image selection without copying secrets into the change
  record.

If the task definition contains only a tag, the ECS running-task digest is the
stronger rollback reference.

## 2. Select and inspect the target

Release notes are published at
<https://github.com/open-webui/open-webui/releases>. Choose an official release
that you have reviewed rather than asserting that the newest release is safe.

Preview resolution without changing AWS resources:

```bash
# Resolve the current latest official release
python3 scripts/resolve-owui-image.py

# Resolve a chosen official tag
python3 scripts/resolve-owui-image.py \
  ghcr.io/open-webui/open-webui:vX.Y.Z
```

Record the returned digest. If resolution warns and returns a tag, stop for any
environment where floating task launches are unacceptable.

### Known integration contracts to compare

This list is not exhaustive; it identifies repository-specific seams that need
explicit attention:

- container startup command and `bash start.sh`;
- `/health` behavior and startup timing;
- supported environment variables for OIDC, S3, Redis/WebSocket state, and
  pgvector;
- database migration compatibility and whether mixed old/new tasks are allowed;
- `config`, `function`, and OAuth-session database models used by the seeders
  and Claude pipe;
- OpenAI connection configuration (`auth_type: system_oauth`, headers,
  `api_type: responses`, prefixes, model IDs);
- OAuth manager/session access used to obtain the user's token;
- global filter inlet/outlet hooks and persisted assistant-message usage shape;
- streaming, tool, image, and usage conventions used by the Claude pipe; and
- Python/boto3 availability used by the runtime bootstrap.

Review upstream source/release changes for each seam. Database, auth, startup,
and usage-persistence changes deserve an isolated deployment even if the UI
looks unchanged.

## 3. Protect the database

Upstream migrations run when the new container starts. There is no separate
migration step in this repository and no guaranteed schema downgrade.

Before deploying:

1. create an Aurora cluster snapshot with a unique, compliant identifier;
2. wait for the snapshot to become `available`;
3. verify point-in-time restore state and retention;
4. record how a restored cluster would be connected to a replacement stack; and
5. read the upstream release's mixed-version guidance.

Example after substituting the verified cluster identifier:

```bash
SNAP="pre-owui-vX-Y-Z-$(date +%Y%m%d-%H%M%S)"

aws rds create-db-cluster-snapshot \
  --db-cluster-identifier YOUR_CLUSTER_ID \
  --db-cluster-snapshot-identifier "$SNAP" \
  --profile YOUR_PROFILE --region us-east-1

aws rds wait db-cluster-snapshot-available \
  --db-cluster-snapshot-identifier "$SNAP" \
  --profile YOUR_PROFILE --region us-east-1
```

A successful image rollback does not reverse a schema migration. The snapshot
or point-in-time restore plan is the database rollback.

## 4. Test outside the main environment

Deploy the target digest to an isolated account/environment with representative
configuration and non-sensitive data. Verify:

- Cognito sign-in, sign-out, group/role mapping, and callback URLs;
- upstream migrations and application health;
- startup seeding of `gw`, `gwr`, the Claude pipe, and the metering filter when
  enabled;
- model discovery and one real streamed response in each non-empty lane;
- WebSocket behavior with more than one task if scale-out matters;
- uploads, retrieval/pgvector, Redis-backed shared state, and application
  restart;
- admin actions and ordinary-user restrictions;
- provider usage persistence and metering settlement when enabled; and
- rollback to the recorded prior digest against an appropriate database copy.

Do not use a model-list HTTP 200 as the only inference check.

## 5. Deploy the target

Set the reviewed digest in `.env`:

```bash
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui@sha256:YOUR_TARGET_DIGEST
```

Run the supported path with every feature flag the deployment already uses:

```bash
./deploy.sh --profile YOUR_PROFILE --region us-east-1

# Metering-enabled deployment:
./deploy.sh --metering --profile YOUR_PROFILE --region us-east-1
```

If scheduled capability refresh is enabled, retain
`ENABLE_MODEL_REFRESH=true` in `.env`. Omitting `--metering` de-wires capture
and admission from newly synthesized Gateway/Compute resources even though an
older Metering stack may remain.

The script prints the selected image. Compare it to the approved digest before
accepting the deployment. The ECS service uses a circuit breaker, healthy-host
alarm, and bake time, but those controls detect deployment health—not every
functional regression.

## 6. Validate after deployment

- [ ] CloudFormation and ECS deployment report complete/stable.
- [ ] Running tasks use the approved image digest.
- [ ] ALB targets remain healthy through the bake window.
- [ ] Cognito login/logout and role/group mapping work.
- [ ] The seeder reports success without blocking application startup.
- [ ] Chat Completions lane lists and streams a response.
- [ ] Responses lane lists and streams a response.
- [ ] Claude discovery and Messages translation work in a region where Claude
      is available.
- [ ] Uploads, retrieval, WebSocket updates, and restart persistence work.
- [ ] If metering is enabled: a representative request creates/settles usage,
      pricing remains fresh, admin/self-service authorization works, and no new
      DLQ/alarm condition appears.
- [ ] Logs contain no new migration, auth, schema, or integration errors.

Record the new digest and validation evidence in the change record.

## 7. Roll back

If the target is unhealthy, the deployment controls may return ECS to the prior
task definition. For a regression discovered later, set the previously recorded
digest in `.env` and run the same supported deployment command/feature flags.

```bash
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui@sha256:PREVIOUS_DIGEST
./deploy.sh --profile YOUR_PROFILE --region us-east-1
# include --metering when applicable
```

Then repeat the validation checklist. If the prior application cannot use the
new schema, do not repeatedly restart it against that database. Execute the
predefined Aurora restore/replacement plan and preserve the failed environment
for diagnosis if policy allows.

## Capability refresh is a separate change

An Open WebUI upgrade does not require a model-capability refresh. Keep image
and model-menu changes separate when possible so failures have one cause.

To update the checked-in snapshot deliberately:

```bash
aws sts get-caller-identity --profile YOUR_PROFILE
python3 scripts/probe-model-capabilities.py \
  --profile YOUR_PROFILE \
  --region us-east-1 \
  --out config/model-capabilities.json \
  --yes

git diff -- config/model-capabilities.json
./deploy.sh --profile YOUR_PROFILE --region us-east-1
```

See the [gateway guide](GATEWAY_INTEGRATION_GUIDE.md#operating-the-model-catalog).

## Historical metering migrations

Older installations may predate the current pricing catalog or console GSIs.
Their migration rationale and one-time procedures are preserved under the
[historical plans index](plans/README.md). Treat those records as version-bound
history: inspect the live table/schema and current CDK before applying a
historical command.

## Change-record minimum

For each upgrade, retain:

- old and new image digests;
- upstream release link and reviewed integration changes;
- database snapshot/restore identifiers;
- enabled deployment flags;
- test environment and results;
- production/evaluation deployment result and alarms;
- post-deploy lane and metering evidence; and
- rollback decision/result, if used.
