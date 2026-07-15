// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

export interface MeteringStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  userPoolClient: cognito.UserPoolClient;
  /** Gateway id (for dashboard links + canary target). */
  gatewayId: string;
  /** Gateway inference base URL (…/inference), for the canaries. */
  gatewayInferenceUrl: string;
  /** Resource-name prefix (e.g. "dev", "prod"). Empty for the default single-env deploy. */
  environmentPrefix?: string;
}

/**
 * Opt-in metering / consumption-tracking / quota-enforcement module.
 *
 * OFF BY DEFAULT. This stack is only synthesized when the `metering` CDK
 * context flag is "on" (deploy.sh --metering). When off, the base three-lane
 * sample is untouched — the lean-core gate in docs/METERING.md verifies the
 * other five stacks synth bit-identically with the flag off.
 *
 * Design + evidence: docs/plans/metering-enforcement/02-DESIGN.md.
 *
 * Populated in phases (docs/plans/metering-enforcement/03-IMPLEMENTATION-RUNBOOK.md):
 *   Phase 1 — data plane: usage ledger/counters table, metering bus,
 *             debit + sweeper + rollup Lambdas, price map.
 *   Phase 2 — enforcement: quota-enforcing gateway interceptor (published via
 *             alias + canary deploy), block/capture canaries.
 *   Phase 3 — attribution: Bedrock Projects provisioner (per-group projects).
 *   Phase 4 — operator surface: admin HTTP API, nightly reconciler, dashboard.
 */
export class MeteringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MeteringStackProps) {
    super(scope, id, props);

    const envPrefix = props.environmentPrefix ? `${props.environmentPrefix}-` : '';

    // Phase 0 shell: the stack exists behind the flag; resources land in Phase 1+.
    new cdk.CfnOutput(this, 'MeteringEnabled', { value: 'true' });
    new cdk.CfnOutput(this, 'MeteringPrefix', { value: `${envPrefix}metering` });
  }
}
