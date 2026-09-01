<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Cost planning

[Documentation home](README.md) · [Deployment guide](AWS_DEPLOYMENT_GUIDE.md) ·
[Metering guide](METERING.md)

This sample has two cost shapes: a baseline AWS application platform and
usage-variable model inference. The optional metering module adds a smaller
control plane. This guide intentionally embeds **no current service prices,
model token rates, monthly totals, or user-count forecasts**; those values vary
by region, model, routing mode, workload, and date.

Use the [AWS Pricing Calculator](https://calculator.aws/) and current official
service pricing pages for a deployment estimate. After deployment, use billing
data and the metering ledger as operational evidence rather than treating a
forecast as an invoice.

## Cost drivers in the base deployment

| Area | Resources created by this sample | Primary drivers to enter in a calculator |
|---|---|---|
| Container compute | ECS service on Fargate; one task by default, autoscaling up to ten outside the dev preset | Average task count, configured vCPU/memory, hours, architecture, data transfer |
| Relational/vector data | Aurora PostgreSQL Serverless v2 writer and reader with pgvector | Minimum/average/peak ACUs, storage, I/O mode, backups, data transfer |
| Shared application state | One ElastiCache Redis node | Node class, hours, backup and transfer requirements |
| Network | Two-AZ VPC, two NAT gateways by default, internal ALB, selected gateway/interface endpoints | NAT hours and processed GB, endpoint-hours per AZ, ALB hours/LCUs, internet and regional transfer |
| Edge delivery | CloudFront distribution with caching disabled for the application behavior | Requests and data transfer; this sample does not claim static-cache savings |
| Identity | Cognito user pool and Managed Login | Monthly active users and any federation/MFA features you add |
| Storage and secrets | S3 uploads, Secrets Manager secrets, CloudWatch logs | Stored GB, requests, retention, retrieval, log ingestion |
| Gateway integration | AgentCore gateway plus provisioner/interceptor Lambdas | Gateway/request pricing, Lambda requests/duration, logs |
| Model inference | Bedrock-compatible models reached through `bedrock-mantle` | Input, output, cache, tier, context, and routing dimensions for each selected model |

The repository builds and stores no application image in ECR. Fargate pulls the
selected official Open WebUI image from GHCR; NAT transfer and registry access
still belong in the network estimate.

## Variable model cost

For each model and pricing dimension, use the current published units. A simple
standard-token forecast is:

```text
monthly model cost =
  (input tokens  ÷ published unit) × current input rate
+ (output tokens ÷ published unit) × current output rate
+ applicable cache, tier, long-context, or routing charges
```

Do not collapse input and output into a blended rate unless you preserve the
assumed ratio and run sensitivity ranges. Conversation history, retrieval
context, tools, images, reasoning, retries, and response length can materially
change that ratio.

Useful starting points:

- [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
- [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/)
- [Amazon Aurora pricing](https://aws.amazon.com/rds/aurora/pricing/)
- [Amazon ElastiCache pricing](https://aws.amazon.com/elasticache/pricing/)
- [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/)
- [Amazon CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/)
- [Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/)

Verify that a model is actually available through the deployment's Mantle
catalog and intended lane before using it in a forecast. The checked-in
capability matrix is a snapshot, not a current catalog promise.

## Optional metering-module cost

`./deploy.sh --metering` adds or activates:

- a DynamoDB on-demand table with streams and global secondary indexes;
- EventBridge, Lambda, SQS DLQ, Scheduler/Rules, SNS, and CloudWatch resources;
- a separate private-S3/CloudFront console and an HTTP API;
- hourly canaries, daily pricing refresh, and nightly reconciliation; and
- a metering interceptor version/alias with a CodeDeploy canary deployment.

Estimate those resources from expected model-call volume, ledger retention,
console traffic, schedules, and log/metric retention. There is no defensible
flat monthly add-on for every workload.

## What the runtime pricing catalog does—and does not do

When metering is enabled, the daily/on-demand pricing refresher parses regional
AWS Price List offers and writes model-keyed rates to DynamoDB. Admission and
settlement use the same resolver, with this precedence:

1. operator override;
2. AWS-published row;
3. unpriced.

The gateway↔pricing coverage join names and alarms on catalog-available models
that cannot be priced. It does **not** block those models or invent a rate;
unpriced calls record tokens and $0 until an operator supplies a defensible
rate or AWS publishes one.

This catalog is useful for operational metering. It is not a substitute for a
budget forecast because it does not predict adoption, concurrency, context
size, data transfer, database growth, support labor, or every billing
adjustment.

## Build a workload-specific estimate

1. **Choose the region and configuration.** Record task sizing, Aurora range,
   Redis class, AZ count, endpoints, retention, custom domain, and whether
   metering is enabled.
2. **Model adoption as ranges.** Estimate monthly active users, sessions per
   active user, turns per session, and peak concurrency. Keep registered users
   separate from active users.
3. **Measure token shape.** Use representative prompts and responses to produce
   low/base/high input and output distributions. Include conversation-history
   growth and retrieval/tool overhead.
4. **Assign traffic by model and lane.** Use only models you have validated for
   the account/region. Run sensitivity for routing, tier, and model mix.
5. **Price the AWS platform.** Enter the resource drivers from the first table
   in the AWS Pricing Calculator. Include two NAT gateways and both Aurora
   instances unless you intentionally change the architecture.
6. **Add operational headroom.** Include logs, backups, data transfer, retries,
   load tests, non-production environments, and incident/recovery activity.
7. **Set monitoring thresholds.** Use AWS Budgets and billing alarms. If
   metering is enabled, configure the pricing catalog and user policies only
   after reviewing its [enforcement boundaries](METERING.md#enforcement-contract).
8. **Reconcile after launch.** Compare estimates with Cost Explorer/CUR and
   measured application usage, then update assumptions rather than preserving a
   stale worked example.

## Cost-control levers

- Limit which models each Open WebUI group can see using native model RBAC.
- Set per-user USD and RPM policies with the optional metering module, while
  accounting for next-request enforcement, fail-soft paths, and unpriced calls.
- Clamp output-token requests and make response limits visible to users.
- Tune Fargate, Aurora, Redis, NAT, endpoint, log, and backup configuration from
  measured demand; do not assume the sample defaults fit a target workload.
- Pin and test an Open WebUI release so an unrelated deployment does not change
  the application version unexpectedly.

## A note on the retired 20,000-user forecast

The previous `COST_ANALYSIS_20K_USERS.md` mixed one education adoption scenario,
hardcoded model prices, competitor comparisons, and infrastructure assumptions
that no longer matched the deployed defaults or model menu. It is retained in
git history as context, not as current guidance. The stable path now points to
this source-driven planning method.
