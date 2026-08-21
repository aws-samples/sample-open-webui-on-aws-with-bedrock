<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Project Structure

```text
infra/                              TypeScript AWS CDK application
  bin/app.ts                        Composition root and stack dependency order
  lib/*-stack.ts                    Network, Data, Auth, Gateway, Metering, and Compute stacks
  lib/environment-config.ts         Environment-specific CDK settings
gateway/
  interceptor/index.py              AgentCore request interceptor/model filtering
  metering-interceptor/index.py     Admission, reservation, quota, and attribution logic
  provisioner/index.py              Inference-target lifecycle custom resource
  refresher/index.py                Optional capability probing and connector refresh
metering/
  pricing/                          Shared pricing resolver and offer normalization
  debit/                            Authoritative usage settlement
  sweeper/, rollup/, reconciler/    Recovery and derived accounting
  admin-api/, canary/               Operator API and health probes
console/                             Optional metering admin/self-service SPA
pipe/
  gateway_anthropic_pipe.py         Claude Messages API manifold pipe
  seed.py                           Seeds the pipe and native OpenAI connections
  metering_filter.py                Captures normalized Open WebUI usage
config/
  model-capabilities.json           Generated API/model compatibility matrix
scripts/
  probe-model-capabilities.py       Regenerates model capability data
  diagnose-model-pricing.py         Runs the production pricing pipeline per model (why (un)priced)
  pricing-rate-diff.py              Pre-deploy effective-rate diff between two pricing snapshots
docs/                               Deployment, integration, metering, cost, and upgrade guides
deploy.sh                           End-to-end deployment/update entry point
.env.example                        Safe application configuration template
```

## Architectural boundaries

- `infra/bin/app.ts` wires Network → Data/Auth → Gateway → optional Metering → Compute; keep orchestration there and resource definitions in the matching `infra/lib/` stack.
- Put gateway request transformation/filtering in `gateway/interceptor/`, admission and reservation in `gateway/metering-interceptor/`, and inference-target lifecycle logic in `gateway/provisioner/` and `gateway/refresher/`.
- Put settlement, pricing, recovery, reconciliation, canaries, and administrative APIs in `metering/` rather than Open WebUI integration code.
- The gateway→pricing join lives in the pricing refresher: it reads served `MODEL_CAPS` + the live gateway catalog and writes a `PRICING#_COVERAGE` item (alarmed via `UnpricedGatewayModels`), keeping metering optional and DynamoDB the single pricing store.
- Put Open WebUI runtime integration, usage capture, and startup seeding in `pipe/`; avoid modifying upstream Open WebUI code.
- Keep model/API compatibility as data in `config/`, with discovery and regeneration logic in `scripts/`.
- Keep operational guidance and runbooks in `docs/`; update them when deployment, metering, or upgrade behavior changes.

## Generated and local-only content

Do not hand-edit or commit `node_modules/`, CDK output (`cdk.out/`, `infra/cdk.out/`), compiled TypeScript (`infra/**/*.js`, `infra/**/*.d.ts`), Python caches/native extensions, `.env`, `infra/deploy.config.json`, `infra/cdk.context.json`, vendored Lambda packages, or `.kiro/hooks/.state/`. The source under `.kiro/hooks/`, `.kiro/steering/`, and `.kiro/specs/` is repository automation and guidance; only hook runtime state is local.
