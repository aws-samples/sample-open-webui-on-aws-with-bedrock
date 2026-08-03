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
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwv2Authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as apigwv2Integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Construct } from 'constructs';
import { MeteringConsole } from './metering-console';

export interface MeteringStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  userPoolClient: cognito.UserPoolClient;
  /**
   * The gateway stack's canary app client (USER_PASSWORD_AUTH, no secret,
   * allow-listed on the gateway). The main app client is SRP+secret and
   * cannot authenticate headlessly — the canaries MUST use this one.
   */
  canaryClient: cognito.UserPoolClient;
  /** Gateway id (dashboard links + canary target). */
  gatewayId: string;
  /** Gateway inference base URL (…/inference), for the canaries. */
  gatewayInferenceUrl: string;
  /** Managed Login domain host (…amazoncognito.com), for the console's OIDC flow. */
  userPoolDomainName: string;
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

    // ── Shared pricing package (metering/pricing/*.py) ─────────────────────
    // Staged into every pricing consumer at synth (single-source design D6):
    // one resolver for the settle path, the admission estimate, and the admin
    // surface, so they can never disagree. Rates live ONLY in the DynamoDB
    // catalog — no price snapshot ships inside any deployment artifact
    // (Requirement 8.2). Unpriced models debit tokens at $0 and raise the
    // UnpricedModel alarm — never a silent guess (design M3/D4).
    const pricingSrcDir = path.join(__dirname, '..', '..', 'metering', 'pricing');
    const stageWithPricing = (prefix: string, handlerDir: string): string => {
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
      fs.copyFileSync(path.join(handlerDir, 'index.py'), path.join(dir, 'index.py'));
      fs.mkdirSync(path.join(dir, 'pricing'));
      for (const f of fs.readdirSync(pricingSrcDir).filter((n) => n.endsWith('.py'))) {
        fs.copyFileSync(path.join(pricingSrcDir, f), path.join(dir, 'pricing', f));
      }
      return dir;
    };
    const debitStagingDir = stageWithPricing(
      'metering-debit-', path.join(__dirname, '..', '..', 'metering', 'debit'));

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
    // (Also serves the console's policy list via state=POLICY rows — D4.)
    this.table.addGlobalSecondaryIndex({
      indexName: 'estimates',
      partitionKey: { name: 'state', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // Console read paths (docs/plans/metering-admin-console/01-DECISIONS.md D4):
    // by-window = spend-ordered USER/GROUP counters for one window (top
    // spenders, near-limit) — writers stamp `w` at settle/sweep/rollup/reset.
    this.table.addGlobalSecondaryIndex({
      indexName: 'by-window',
      partitionKey: { name: 'w', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'used_usd', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // by-sub = one user's ledger history, newest first (ledger sk = <epoch>#<key>).
    // NOTE (upgrades): DynamoDB permits ONE GSI add per stack update. Fresh
    // deploys create both console GSIs at once; upgrading a pre-console
    // metering deployment needs two passes — deploy once with
    // -c meteringGsiPhase=1 (adds by-window), then once without (adds by-sub).
    if (this.node.tryGetContext('meteringGsiPhase') !== '1') {
      this.table.addGlobalSecondaryIndex({
        indexName: 'by-sub',
        partitionKey: { name: 'sub', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
        projectionType: dynamodb.ProjectionType.ALL,
      });
    }

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
      code: lambda.Code.fromAsset(debitStagingDir),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        TABLE: this.table.tableName,
        SNS_TOPIC: this.alertsTopic.topicArn,
        GROUP_ORDER: JSON.stringify(
          JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'config', 'metering-groups.json'), 'utf-8')).groups ?? [],
        ),
        HARD_DEFAULT_USD: '5',
        SOFT_DEFAULT_USD: '4',
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
      description: 'Usage events from the seeded metering filter (the interceptor writes estimates to DynamoDB directly)',
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

    // ── Canaries: prove BOTH failure directions hourly (design §4.2/§4.8) ──
    // Block canary: over-quota synthetic user must get 429. Capture canary:
    // a usage event must settle. They authenticate as real Cognito users so
    // the whole JWT→interceptor→DDB path is exercised.
    const canaryPassword = new secretsmanager.Secret(this, 'CanaryPassword', {
      secretName: `open-webui/${envPrefix}metering-canary-password`,
      generateSecretString: { excludePunctuation: false, passwordLength: 24 },
    });
    const gatewayUrlParam = new ssm.StringParameter(this, 'GatewayUrlParam', {
      parameterName: `/${envPrefix || 'default-'}open-webui/metering/gateway-inference-url`,
      stringValue: props.gatewayInferenceUrl,
    });
    const canaryEnvCommon = {
      TABLE: this.table.tableName,
      BUS: this.bus.eventBusName,
      USER_POOL_ID: props.userPool.userPoolId,
      CLIENT_ID: props.canaryClient.userPoolClientId,
      PASSWORD_SECRET_ARN: canaryPassword.secretArn,
      GATEWAY_URL_PARAM: gatewayUrlParam.parameterName,
    };
    const mkCanary = (mode: 'block' | 'capture') => {
      const fn = new lambda.Function(this, `${mode}Canary`, {
        functionName: `${envPrefix}open-webui-metering-${mode}-canary`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.handler',
        code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'metering', 'canary')),
        timeout: cdk.Duration.minutes(2),
        memorySize: 256,
        environment: { ...canaryEnvCommon, MODE: mode, USERNAME: `metering-${mode}-canary@example.invalid` },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      this.table.grantReadWriteData(fn);
      this.bus.grantPutEventsTo(fn);
      canaryPassword.grantRead(fn);
      gatewayUrlParam.grantRead(fn);
      fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['cognito-idp:AdminGetUser', 'cognito-idp:AdminCreateUser',
                  'cognito-idp:AdminSetUserPassword', 'cognito-idp:InitiateAuth'],
        resources: [props.userPool.userPoolArn],
      }));
      fn.addToRolePolicy(metricsPolicy);
      new events.Rule(this, `${mode}CanarySchedule`, {
        schedule: events.Schedule.rate(cdk.Duration.hours(1)),
        targets: [new targets.LambdaFunction(fn)],
      });
      const alarm = new cloudwatch.Alarm(this, `${mode}CanaryAlarm`, {
        alarmName: `${envPrefix}open-webui-metering-${mode}-canary`,
        metric: new cloudwatch.Metric({
          namespace: 'Metering',
          metricName: mode === 'block' ? 'BlockCanaryFailure' : 'CaptureCanaryFailure',
          statistic: 'Sum',
          period: cdk.Duration.hours(1),
        }),
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
      alarm.addAlarmAction(new cwactions.SnsAction(this.alertsTopic));
      return fn;
    };
    mkCanary('block');
    mkCanary('capture');

    // ── Reconciler: nightly ledger-vs-invoice drift (design M3) ────────────
    const reconcilerFn = new lambda.Function(this, 'ReconcilerFn', {
      functionName: `${envPrefix}open-webui-metering-reconciler`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'metering', 'reconciler')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: { TABLE: this.table.tableName, FLOOR_TOKENS: '100000' },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    this.table.grantReadData(reconcilerFn);
    reconcilerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ce:GetCostAndUsage'],
      resources: ['*'],
    }));
    reconcilerFn.addToRolePolicy(metricsPolicy);
    new events.Rule(this, 'ReconcilerSchedule', {
      schedule: events.Schedule.cron({ minute: '0', hour: '6' }),
      targets: [new targets.LambdaFunction(reconcilerFn)],
    });
    const driftAlarm = new cloudwatch.Alarm(this, 'DriftAlarm', {
      alarmName: `${envPrefix}open-webui-metering-reconciliation-drift`,
      metric: new cloudwatch.Metric({
        namespace: 'Metering', metricName: 'ReconciliationDriftPct',
        dimensionsMap: { Model: 'ALL' }, statistic: 'Maximum', period: cdk.Duration.days(1),
      }),
      threshold: 5,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    driftAlarm.addAlarmAction(new cwactions.SnsAction(this.alertsTopic));

    // ── Pricing refresher: the single automated price source ───────────────
    //    (.kiro/specs/metering-pricing-single-source/design.md). Reads the
    //    three Bedrock offer files for this region (public HTTPS, no auth) and
    //    bedrock:ListFoundationModels, writes model-id-keyed PRICING# rows and
    //    the _UNMATCHED review queue. Operator overrides (PRICING#/OVERRIDE)
    //    are written by the admin API, win at settle time, and are never
    //    touched by a refresh.
    const refresherStagingDir = stageWithPricing(
      'metering-pricing-refresher-', path.join(__dirname, '..', '..', 'metering', 'pricing-refresher'));
    const pricingRefresherFn = new lambda.Function(this, 'PricingRefresherFn', {
      functionName: `${envPrefix}open-webui-metering-pricing-refresher`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(refresherStagingDir),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: { TABLE: this.table.tableName, REGION: cdk.Aws.REGION },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    this.table.grantReadWriteData(pricingRefresherFn);
    pricingRefresherFn.addToRolePolicy(metricsPolicy);
    // Model-id join needs the control-plane listing — read-only, no inference
    // or mutation actions, no resource-level scoping exists for List* (Req 11.1).
    pricingRefresherFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:ListFoundationModels'],
      resources: ['*'],
    }));
    // Daily refresh (published rates change infrequently; overrides are instant).
    new events.Rule(this, 'PricingRefreshSchedule', {
      schedule: events.Schedule.rate(cdk.Duration.hours(24)),
      targets: [new targets.LambdaFunction(pricingRefresherFn)],
    });
    const pricingRefreshAlarm = new cloudwatch.Alarm(this, 'PricingRefreshAlarm', {
      alarmName: `${envPrefix}open-webui-metering-pricing-refresh`,
      metric: new cloudwatch.Metric({
        namespace: 'Metering', metricName: 'PricingRefreshFailure',
        statistic: 'Sum', period: cdk.Duration.hours(24),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pricingRefreshAlarm.addAlarmAction(new cwactions.SnsAction(this.alertsTopic));
    // Unresolved published rates are a work queue, not a page: warn so the
    // operator binds them in the console (Req 3.3, design §8).
    const pricingUnmatchedAlarm = new cloudwatch.Alarm(this, 'PricingUnmatchedAlarm', {
      alarmName: `${envPrefix}open-webui-metering-pricing-unmatched`,
      alarmDescription: 'Price List entries with no resolvable Bedrock model id — review the Unmatched queue on the console pricing page.',
      metric: new cloudwatch.Metric({
        namespace: 'Metering', metricName: 'PricingUnmatched',
        statistic: 'Maximum', period: cdk.Duration.hours(24),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    pricingUnmatchedAlarm.addAlarmAction(new cwactions.SnsAction(this.alertsTopic));

    // ── Admin API: the operator control surface, outside Open WebUI ────────
    // Serves the CLI (scripts/set-quota.sh) AND the admin console (console/).
    const enforceMode = this.node.tryGetContext('meteringMode') === 'observe' ? 'OBSERVE' : 'ENFORCE';
    const adminFn = new lambda.Function(this, 'AdminFn', {
      functionName: `${envPrefix}open-webui-metering-admin`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(stageWithPricing(
        'metering-admin-', path.join(__dirname, '..', '..', 'metering', 'admin-api'))),
      timeout: cdk.Duration.seconds(15),
      memorySize: 512,
      environment: {
        TABLE: this.table.tableName,
        USER_POOL_ID: props.userPool.userPoolId,
        ALERTS_TOPIC_ARN: this.alertsTopic.topicArn,
        ALARM_PREFIX: `${envPrefix}open-webui-metering`,
        ENFORCE_MODE: enforceMode,
        PRICING_REFRESHER_FN: pricingRefresherFn.functionName,
        HARD_DEFAULT_USD: '5',
        SOFT_DEFAULT_USD: '4',
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    this.table.grantReadWriteData(adminFn);
    // Admin can trigger an on-demand pricing refresh from the console.
    pricingRefresherFn.grantInvoke(adminFn);
    // Console read surfaces beyond the table — all read-only, resource-scoped
    // where the service supports it (GetMetricData has no resource ARNs).
    adminFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:AdminGetUser', 'cognito-idp:AdminListGroupsForUser', 'cognito-idp:ListUsers'],
      resources: [props.userPool.userPoolArn],
    }));
    adminFn.addToRolePolicy(new iam.PolicyStatement({
      // Both are read-only. GetMetricData has no resource-level scoping at
      // all; DescribeAlarms-by-prefix authorizes against alarm:* (the exact
      // names aren't known at authorization time), so alarm:* is the
      // tightest expressible resource — the Lambda still only asks for the
      // module's prefix.
      actions: ['cloudwatch:GetMetricData'],
      resources: ['*'],
    }));
    adminFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:DescribeAlarms'],
      resources: [`arn:aws:cloudwatch:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:alarm:*`],
    }));
    adminFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['sns:ListSubscriptionsByTopic', 'sns:Subscribe', 'sns:Unsubscribe'],
      resources: [this.alertsTopic.topicArn],
    }));

    const httpApi = new apigwv2.HttpApi(this, 'AdminApi', {
      apiName: `${envPrefix}open-webui-metering-admin`,
      description: 'Metering operator API (quotas, usage, overrides) — Cognito-JWT-gated',
    });

    // ── Admin console: static SPA + PKCE client + same-origin /api/* (D1/D2) ─
    const console_ = new MeteringConsole(this, 'Console', {
      userPool: props.userPool,
      userPoolDomainName: props.userPoolDomainName,
      httpApi,
    });

    const authorizer = new apigwv2Authorizers.HttpJwtAuthorizer(
      'CognitoJwt',
      `https://cognito-idp.${cdk.Aws.REGION}.amazonaws.com/${props.userPool.userPoolId}`,
      // Three app clients: OWUI (real admins hold its tokens), canary/CLI
      // (headless operators + verification tooling), and the console's PKCE
      // client. Group membership — not client choice — gates admin routes.
      {
        jwtAudience: [
          props.userPoolClient.userPoolClientId,
          props.canaryClient.userPoolClientId,
          console_.client.userPoolClientId,
        ],
      },
    );
    const integration = new apigwv2Integrations.HttpLambdaIntegration('AdminIntegration', adminFn);
    for (const [routePath, methods] of [
      ['/policy/{scope}', [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE]],
      ['/policies', [apigwv2.HttpMethod.GET]],
      ['/usage/{sub}', [apigwv2.HttpMethod.GET]],
      ['/usage/me', [apigwv2.HttpMethod.GET]],
      ['/user/me/ledger', [apigwv2.HttpMethod.GET]],
      ['/users', [apigwv2.HttpMethod.GET]],
      ['/users/search', [apigwv2.HttpMethod.GET]],
      ['/user/{sub}', [apigwv2.HttpMethod.GET]],
      ['/user/{sub}/ledger', [apigwv2.HttpMethod.GET]],
      ['/groups', [apigwv2.HttpMethod.GET]],
      ['/audit', [apigwv2.HttpMethod.GET]],
      ['/activity', [apigwv2.HttpMethod.GET]],
      ['/estimates', [apigwv2.HttpMethod.GET]],
      ['/metrics', [apigwv2.HttpMethod.GET]],
      ['/alarms', [apigwv2.HttpMethod.GET]],
      ['/alert-subscriptions', [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE]],
      ['/config', [apigwv2.HttpMethod.GET]],
      ['/pricing', [apigwv2.HttpMethod.GET]],
      ['/pricing/{model}', [apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE]],
      ['/pricing/refresh', [apigwv2.HttpMethod.POST]],
      ['/pricing/alias', [apigwv2.HttpMethod.POST]],
      ['/pricing/alias/{name}', [apigwv2.HttpMethod.DELETE]],
      ['/override', [apigwv2.HttpMethod.POST]],
      ['/counter-reset', [apigwv2.HttpMethod.POST]],
    ] as const) {
      httpApi.addRoutes({ path: routePath, methods: [...methods], integration, authorizer });
    }

    // ── Ops dashboard ───────────────────────────────────────────────────────
    const m = (name: string, stat = 'Sum', dims?: Record<string, string>) =>
      new cloudwatch.Metric({ namespace: 'Metering', metricName: name, statistic: stat, period: cdk.Duration.minutes(15), dimensionsMap: dims });
    new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `${envPrefix}open-webui-metering`,
      widgets: [[
        new cloudwatch.GraphWidget({ title: 'Spend (settled USD / 15 min)', left: [m('SettledUSD')], width: 8 }),
        new cloudwatch.GraphWidget({ title: 'Calls settled / denies', left: [m('SettledCalls'), m('DenyDecisions')], width: 8 }),
        new cloudwatch.GraphWidget({ title: 'Degraded checks (fail-open)', left: [m('DegradedChecks')], width: 8 }),
      ], [
        new cloudwatch.GraphWidget({ title: 'Sweeper (orphaned estimates)', left: [m('SweptEstimates'), m('SweptEstimateUSD')], width: 8 }),
        new cloudwatch.GraphWidget({ title: 'Canaries (failures)', left: [m('BlockCanaryFailure'), m('CaptureCanaryFailure')], width: 8 }),
        new cloudwatch.GraphWidget({ title: 'Reconciliation drift % (ALL)', left: [m('ReconciliationDriftPct', 'Maximum', { Model: 'ALL' })], width: 8 }),
      ]],
    });

    // ── Outputs ────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'MeteringTableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'MeteringBusName', { value: this.bus.eventBusName });
    new cdk.CfnOutput(this, 'MeteringAlertsTopicArn', { value: this.alertsTopic.topicArn });

    new cdk.CfnOutput(this, 'AdminApiUrl', { value: httpApi.apiEndpoint });
  }
}
