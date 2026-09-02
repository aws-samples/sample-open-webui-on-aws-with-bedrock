<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Documentation

Use this page as the front door for current guidance. The files under
[`plans/`](plans/), [`reviews/`](reviews/), and [`solutions/`](solutions/) are
point-in-time project records; they explain how decisions were reached but are
not deployment instructions.

## Choose a path

### Evaluate

1. Start with the [repository overview](../README.md) for the value proposition,
   canonical architecture, and constraints.
2. Read the [gateway integration guide](GATEWAY_INTEGRATION_GUIDE.md) to check
   the identity boundary, three API lanes, model-list lifecycle, and regional
   assumptions.
3. Use [cost planning](COSTS.md) to build a region- and workload-specific
   estimate without relying on embedded token prices.
4. If governance matters, read the contract and known limits at the beginning
   of [Metering, consumption governance, and quotas](METERING.md).

### Deploy and validate

Follow the [AWS deployment guide](AWS_DEPLOYMENT_GUIDE.md) from prerequisites
through configuration, deployment, browser validation, operations, and cleanup.
It is the supported consumer deployment path and owns troubleshooting.

### Operate and change

- [Metering, consumption governance, and quotas](METERING.md) — enablement,
  policy semantics, pricing coverage, console behavior, alarms, recovery, and
  failure posture.
- [Open WebUI upgrade runbook](UPGRADE_RUNBOOK.md) — select or pin an upstream
  release, protect the database, validate integration contracts, and roll back.
- [Gateway integration guide](GATEWAY_INTEGRATION_GUIDE.md#operating-the-model-catalog)
  — regenerate or schedule the model-capability snapshot.

### Maintain the repository

- [`infra/README.md`](../infra/README.md) — CDK composition, context, and
  infrastructure validation.
- [`pipe/README.md`](../pipe/README.md) — runtime-seeded Open WebUI integration
  files.
- [`console/README.md`](../console/README.md) — metering console build and local
  development.
- [`diagrams/README.md`](diagrams/README.md) — canonical diagram sources and
  deterministic light/dark rendering.
- [`images/SCREENSHOT-SPEC.md`](images/SCREENSHOT-SPEC.md) — provenance and safe refresh procedure for the committed Open WebUI frames and remaining governance-console captures.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — contribution and validation path.

## Current document ownership

| Document | Owns | Does not own |
|---|---|---|
| [`../README.md`](../README.md) | Evaluation front door and reading paths | Full deployment or operator procedures |
| [`GATEWAY_INTEGRATION_GUIDE.md`](GATEWAY_INTEGRATION_GUIDE.md) | Canonical architecture, identity, lanes, capability lifecycle | General deployment procedure |
| [`AWS_DEPLOYMENT_GUIDE.md`](AWS_DEPLOYMENT_GUIDE.md) | Prerequisites, deploy, validate, operate, troubleshoot, cleanup | Independent architecture narrative |
| [`METERING.md`](METERING.md) | Governance contract and metering operations | Base deployment procedure |
| [`COSTS.md`](COSTS.md) | Cost drivers, formulas, estimation workflow | Current service or model prices |
| [`UPGRADE_RUNBOOK.md`](UPGRADE_RUNBOOK.md) | Upstream image lifecycle and rollback | General application operations |

The legacy path [`COST_ANALYSIS_20K_USERS.md`](COST_ANALYSIS_20K_USERS.md) is a
compatibility pointer. Its former customer-specific forecast is available in
git history, not maintained as current pricing guidance.

## Project history and maintainer records

- [`plans/`](plans/README.md) — dated designs, investigations, and decision logs.
- [`reviews/`](reviews/README.md) — point-in-time review evidence and findings.
- [`solutions/`](solutions/README.md) — captured implementation patterns and
  learnings.

Historical records can contain superseded assumptions, measured snapshots, and
commands from the period in which they were written. Resolve any conflict in
favor of current source code and the current guides above.

## Repository policies

- [Security reporting](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
- [Code of Conduct](../CODE_OF_CONDUCT.md)
- [MIT-0 license](../LICENSE)
- [Third-party notices](../THIRD-PARTY-LICENSES.md)
