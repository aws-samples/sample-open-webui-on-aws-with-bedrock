<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Historical plans and decision records

[Documentation home](../README.md)

These files preserve point-in-time research, measurements, designs,
implementation runbooks, and rejected alternatives. They are **history, not
current deployment or operator guidance**. Some commands, prices, model lists,
line references, or assumptions were valid only when written.

Resolve conflicts in favor of current source code and these current guides:

- [Gateway integration](../GATEWAY_INTEGRATION_GUIDE.md)
- [Metering contract and operations](../METERING.md)
- [Deployment](../AWS_DEPLOYMENT_GUIDE.md)
- [Cost planning](../COSTS.md)
- [Upgrade runbook](../UPGRADE_RUNBOOK.md)

## Metering enforcement investigation

Read in numeric order:

1. [`metering-enforcement/00-MORNING-REPORT.md`](metering-enforcement/00-MORNING-REPORT.md)
2. [`metering-enforcement/01-LANDSCAPE.md`](metering-enforcement/01-LANDSCAPE.md)
3. [`metering-enforcement/02-DESIGN.md`](metering-enforcement/02-DESIGN.md)
4. [`metering-enforcement/03-IMPLEMENTATION-RUNBOOK.md`](metering-enforcement/03-IMPLEMENTATION-RUNBOOK.md)
5. [`metering-enforcement/04-SPIKE-FINDINGS.md`](metering-enforcement/04-SPIKE-FINDINGS.md)

Supporting research tracks:

- [`track-a-aws-primitives.md`](metering-enforcement/research/track-a-aws-primitives.md)
- [`track-b-prior-art.md`](metering-enforcement/research/track-b-prior-art.md)
- [`track-c-gateway-internals.md`](metering-enforcement/research/track-c-gateway-internals.md)
- [`track-d-reconciliation.md`](metering-enforcement/research/track-d-reconciliation.md)

## Metering console and pricing decisions

Read in numeric order:

1. [`01-DECISIONS.md`](metering-admin-console/01-DECISIONS.md)
2. [`02-PRICING-INVESTIGATION.md`](metering-admin-console/02-PRICING-INVESTIGATION.md)
3. [`03-PRICING-RECONCILIATION.md`](metering-admin-console/03-PRICING-RECONCILIATION.md)
4. [`04-PROVIDER-PRICE-SOURCE.md`](metering-admin-console/04-PROVIDER-PRICE-SOURCE.md)
5. [`05-PRICING-SINGLE-SOURCE.md`](metering-admin-console/05-PRICING-SINGLE-SOURCE.md)
6. [`06-GATEWAY-PRICING-COVERAGE.md`](metering-admin-console/06-GATEWAY-PRICING-COVERAGE.md)
7. [`07-REJECTED-PATTERNS.md`](metering-admin-console/07-REJECTED-PATTERNS.md)

The current operator contract superseding these records is
[`../METERING.md`](../METERING.md).

## Pricing-catalog brief

- [`metering-pricing-catalog/00-FABLE-BRIEF.md`](metering-pricing-catalog/00-FABLE-BRIEF.md)

This is the initiating brief for a completed design pass, not a current
requirement or runbook.
