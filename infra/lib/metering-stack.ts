// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { DynamoEventSource } from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

export interface MeteringStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  userPoolClient: cognito.UserPoolClient;
  /** Gateway id (dashboard links + canary target). */
  gatewayId: string;
  /** Gateway inference base URL (…/inference), for the canaries. */
  gatewayInferenceUrl: string;
  /** Resource-name prefix (e.g. "dev", "prod"). Empty for the default single-env deploy. */
  environmentPrefix?: string;
}

/**
 * Opt-in metering / consumption-tracking / quota-enforcement module.
 *
 * OFF BY DEFAULT — synthesized only when the `metering` CDK context flag is
 * "on" (deploy.sh --metering). When off, the base three-lane sample is
 * untouched (lean-core gate: docs/METERING.md).
 *
 * Data model (single table, docs/plans/metering-enforcement/02-DESIGN.md §4.3):
 *   POLICY#<scope> / POLICY        quota policies (DEFAULT, GROUP#g, USER#sub)
 *   USE#<sub>#<yyyy-mm> / COUNTER  per-user window counters (used + estimates)
 *   GROUP#<g>#<yyyy-mm> / COUNTER  async group rollups (ceilings, not chargeback)
 *   RPM#<sub>#<minute> / RPM       per-minute rate buckets (TTL)
 *   EST#<key> / EST                open admission estimates (sweeper-resolved)
 *   LEDGER#<yyyy-mm-dd> / ts#key   append-only usage ledger (TTL ~15 months)
 *   AUDIT#<yyyy-mm-dd> / ts#actor  admin-action audit records
 */
export class MeteringStack extends cdk.Stack {
  public readonly table: dynamodb.Table;
  public readonly bus: events.EventBus;
  public readonly alertsTopic: sns.Topic;
  /** Interceptor alias consumed by the gateway stack when metering is on (Phase 2). */
  public meteringInterceptorAlias?: lambda.Alias;

  constructor(scope: Construct, id: string, props: MeteringStackProps) {
    super(scope, id, props);

    const envPrefix = props.environmentPrefix ? `${props.environmentPrefix}-` : '';

    // ── Price map: generated from the AWS Price List offer file ───────────
    // (scripts/generate-price-map.py). Unpriced models debit tokens at $0 and
    // raise the UnpricedModel alarm — never a silent guess (design M3).
    const pricesPath = path.join(__dirname, '..', '..', 'config', 'model-prices.json');
    const priceMap = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
    const priceMapEnv = JSON.stringify({ version: priceMap.version, models: { ...priceMap.models, ...priceMap.overrides } });

    // ── The single metering table ──────────────────────────────────────────
    this.table = new dynamodb.Table(this, 'MeteringTable', {
      tableName: `${envPrefix}open-webui-metering`,
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
    });
    // Sweeper lookup: OPEN estimates older than the max stream duration.
    this.table.addGlobalSecondaryIndex({
      indexName: 'estimates',
      partitionKey: { name: 'state', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── Metering bus + alerts topic ────────────────────────────────────────
    this.bus = new events.EventBus(this, 'MeteringBus', {
      eventBusName: `${envPrefix}open-webui-metering`,
    });
    this.alertsTopic = new sns.Topic(this, 'MeteringAlerts', {
      topicName: `${envPrefix}open-webui-metering-alerts`,
    });

    const metricsPolicy = new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': 'Metering' } },
    });

    // ── Debit Lambda: settle usage events (transactional, idempotent) ─────
    const debitDlq = new sqs.Queue(this, 'DebitDlq', {
      queueName: `${envPrefix}open-webui-metering-debit-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });
    const debitFn = new lambda.Function(this, 'DebitFn', {
      functionName: `${envPrefix}open-webui-metering-debit`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'metering', 'debit')),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        TABLE: this.table.tableName,
        PRICE_MAP: priceMapEnv,
        SNS_TOPIC: this.alertsTopic.topicArn,
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
      deadLetterQueue: debitDlq,
    });
    // Debit writes ledger/EST/counters + reads estimates — table-scoped only.
    this.table.grantReadWriteData(debitFn);
    this.alertsTopic.grantPublish(debitFn);
    debitFn.addToRolePolicy(metricsPolicy);

    new events.Rule(this, 'UsageRule', {
      eventBus: this.bus,
      description: 'Usage events from the seeded metering filter and the gateway interceptor',
      eventPattern: { source: ['openwebui.metering'], detailType: ['usage'] },
      targets: [
        new targets.LambdaFunction(debitFn, {
          deadLetterQueue: debitDlq,
          retryAttempts: 2,
        }),
      ],
    });

    // ── Sweeper: resolve orphaned admission estimates (refund by default) ──
    const sweeperFn = new lambda.Function(this, 'SweeperFn', {
      functionName: `${envPrefix}open-webui-metering-sweeper`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'metering', 'sweeper')),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      environment: {
        TABLE: this.table.tableName,
        MAX_STREAM_SECONDS: '900',
        SWEEPER_MODE: 'refund', // strict deployments: 'settle' (docs/METERING.md)
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    this.table.grantReadWriteData(sweeperFn);
    sweeperFn.addToRolePolicy(metricsPolicy);
    new events.Rule(this, 'SweeperSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(sweeperFn)],
    });

    // ── Rollup: group counters from the table stream (outside the settle tx) ─
    const rollupFn = new lambda.Function(this, 'RollupFn', {
      functionName: `${envPrefix}open-webui-metering-rollup`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'metering', 'rollup')),
      timeout: cdk.Duration.minutes(1),
      memorySize: 256,
      environment: { TABLE: this.table.tableName },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    this.table.grantWriteData(rollupFn);
    rollupFn.addEventSource(
      new DynamoEventSource(this.table, {
        startingPosition: lambda.StartingPosition.LATEST,
        batchSize: 100,
        maxBatchingWindow: cdk.Duration.seconds(10),
        retryAttempts: 3,
        bisectBatchOnError: true,
        filters: [
          lambda.FilterCriteria.filter({
            eventName: lambda.FilterRule.isEqual('INSERT'),
            dynamodb: { NewImage: { pk: { S: lambda.FilterRule.beginsWith('LEDGER#') } } },
          }),
        ],
      }),
    );

    // ── Ops alarms (Phase 1 set; more land with the interceptor in Phase 2) ─
    const dlqAlarm = new cloudwatch.Alarm(this, 'DebitDlqAlarm', {
      alarmName: `${envPrefix}open-webui-metering-debit-dlq`,
      metric: debitDlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(5) }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    dlqAlarm.addAlarmAction(new cwactions.SnsAction(this.alertsTopic));

    const unpricedAlarm = new cloudwatch.Alarm(this, 'UnpricedModelAlarm', {
      alarmName: `${envPrefix}open-webui-metering-unpriced-model`,
      metric: new cloudwatch.Metric({
        namespace: 'Metering', metricName: 'UnpricedModel',
        statistic: 'Sum', period: cdk.Duration.minutes(15),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    unpricedAlarm.addAlarmAction(new cwactions.SnsAction(this.alertsTopic));

    // ── Outputs ────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'MeteringTableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'MeteringBusName', { value: this.bus.eventBusName });
    new cdk.CfnOutput(this, 'MeteringAlertsTopicArn', { value: this.alertsTopic.topicArn });
    new cdk.CfnOutput(this, 'PriceMapVersion', { value: String(priceMap.version) });
  }
}
