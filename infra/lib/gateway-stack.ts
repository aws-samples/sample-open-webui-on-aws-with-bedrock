// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as codedeploy from 'aws-cdk-lib/aws-codedeploy';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Construct } from 'constructs';

export interface GatewayStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  userPoolClient: cognito.UserPoolClient;
  /** Resource-name prefix (e.g. "dev", "prod"). Empty for the default single-env deploy. */
  environmentPrefix?: string;
  /** Region whose bedrock-mantle endpoint the gateway fronts (defaults to the stack region). */
  mantleRegion?: string;
  /**
   * Opt-in metering module (docs/METERING.md): when set, the gateway's REQUEST
   * interceptor is the quota-enforcing v2 (gateway/metering-interceptor/),
   * published behind an alias with CodeDeploy canary traffic-shifting. Absent
   * (the default) ⇒ the base capability-filter interceptor, untouched.
   */
  metering?: boolean;
}

/**
 * AgentCore Gateway that fronts Amazon Bedrock's OpenAI-compatible endpoint
 * (bedrock-mantle) as a single, governed inference endpoint for Open WebUI.
 *
 *  - Inbound auth: CUSTOM_JWT trusting this deployment's Cognito user pool, so
 *    every model call carries the *logged-in user's own* OAuth token (Open WebUI
 *    connections use auth_type "system_oauth"). Per-user identity, ready for
 *    Cedar policies and per-user throttling on model traffic.
 *  - Outbound auth: the gateway's execution role (GATEWAY_IAM_ROLE) signs
 *    requests to bedrock-mantle. No API keys.
 *  - A REQUEST interceptor Lambda narrows each native /v1/models listing to
 *    the probed capability snapshot (config/model-capabilities.json). The
 *    snapshot reduces API-lane mismatches but is not a permanent availability
 *    guarantee.
 *  - The bedrock-mantle inference *target* is created via a custom resource
 *    (no native CFN type for inference targets yet).
 */
export class GatewayStack extends cdk.Stack {
  public readonly gatewayUrl: string;
  public readonly gatewayId: string;
  public readonly gatewayInferenceUrl: string;
  /** Set only when the metering module is on (interceptor v2 behind an alias). */
  public meteringInterceptorAlias?: lambda.Alias;
  /**
   * The metering interceptor Lambda (unqualified), only when metering is on.
   * The metering stack's pricing refresher reads its MODEL_CAPS env var
   * (GetFunctionConfiguration) for the gateway↔pricing coverage join.
   */
  public meteringInterceptorFn?: lambda.Function;
  /** Canary app client (USER_PASSWORD_AUTH), only when metering is on. */
  public canaryClient?: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id, props);

    const envPrefix = props.environmentPrefix ? `${props.environmentPrefix}-` : '';
    const mantleRegion = props.mantleRegion ?? cdk.Aws.REGION;
    const discoveryUrl =
      `https://cognito-idp.${cdk.Aws.REGION}.amazonaws.com/${props.userPool.userPoolId}/.well-known/openid-configuration`;

    // ── Capability matrix → interceptor env ───────────────────────────────
    const capsPath = path.join(__dirname, '..', '..', 'config', 'model-capabilities.json');
    const capsRaw = JSON.parse(fs.readFileSync(capsPath, 'utf-8'));
    const modelCaps = {
      chat_completions: capsRaw.chat_completions ?? [],
      responses: capsRaw.responses ?? [],
      messages: capsRaw.messages ?? [],
    };

    // ── Interceptor Lambda ──────────────────────────────────────────────────
    // Base sample: capability-filtered model listing only. Metering module ON:
    // the quota-enforcing v2 (same models behavior + enforcement; see
    // gateway/metering-interceptor/index.py), published as a VERSION behind an
    // ALIAS with CodeDeploy canary traffic-shifting — an interceptor deploy is
    // in the critical path of every chat, so new code takes 10% of traffic for
    // 5 minutes and auto-rolls-back on errors (docs/METERING.md).
    let interceptorInvokeArn: string;
    let interceptor: lambda.Function;
    if (props.metering) {
      // Canary app client: the metering canaries authenticate as REAL pool
      // users via USER_PASSWORD_AUTH (the main app client is SRP+secret,
      // unusable headlessly). Added to the gateway's AllowedClients below so
      // canary JWTs pass inbound auth like any user's.
      this.canaryClient = new cognito.UserPoolClient(this, 'MeteringCanaryClient', {
        userPool: props.userPool,
        userPoolClientName: `${envPrefix}metering-canary`,
        generateSecret: false,
        authFlows: { userPassword: true },
        accessTokenValidity: cdk.Duration.minutes(60),
      });

      // Bedrock (mantle) Projects per cost-center group — the attribution
      // spine (design M2). Provisioned HERE (not in the metering stack) so the
      // group→project map resolves into the interceptor's env at deploy time:
      // the alias pins a published VERSION whose env is frozen, so post-deploy
      // env mutation from another stack would never reach live traffic.
      const groupsCfg = JSON.parse(
        fs.readFileSync(path.join(__dirname, '..', '..', 'config', 'metering-groups.json'), 'utf-8'),
      );
      const projectsFn = new lambda.Function(this, 'ProjectsProvisioner', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.handler',
        code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'metering', 'projects-provisioner')),
        timeout: cdk.Duration.minutes(2),
        memorySize: 256,
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      // bedrock-mantle is its own IAM service prefix and the projects API's
      // action vocabulary is not yet documented — scoped Project* names were
      // rejected (401 access_denied) in deploy-verify, so match the gateway
      // execution role's precedent (bedrock-mantle:*).
      projectsFn.addToRolePolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:*'],
        resources: ['*'],
      }));
      const projectsProvider = new cr.Provider(this, 'ProjectsProvider', { onEventHandler: projectsFn });
      const projects = new cdk.CustomResource(this, 'MeteringProjects', {
        serviceToken: projectsProvider.serviceToken,
        properties: {
          Groups: JSON.stringify(groupsCfg.groups ?? []),
          NamePrefix: `${envPrefix}owui-metering-`,
          Region: mantleRegion,
          AppTag: 'open-webui-sample',
        },
      });

      const v2Dir = path.join(__dirname, '..', '..', 'gateway', 'metering-interceptor');
      const stagingDir = fs.mkdtempSync(path.join(os.tmpdir(), 'metering-interceptor-'));
      // Bundle the big JSON config beside the handler (4 KB env ceiling) and
      // the shared pricing package (metering/pricing) — the admission estimate
      // resolves rates from the DynamoDB catalog through the same resolver as
      // the settle path; no bundled price snapshot (single-source design D6).
      fs.copyFileSync(path.join(v2Dir, 'index.py'), path.join(stagingDir, 'index.py'));
      fs.copyFileSync(capsPath, path.join(stagingDir, 'model-capabilities.json'));
      const pricingSrcDir = path.join(__dirname, '..', '..', 'metering', 'pricing');
      fs.mkdirSync(path.join(stagingDir, 'pricing'));
      for (const f of fs.readdirSync(pricingSrcDir).filter((n) => n.endsWith('.py'))) {
        fs.copyFileSync(path.join(pricingSrcDir, f), path.join(stagingDir, 'pricing', f));
      }
      interceptor = new lambda.Function(this, 'MeteringInterceptor', {
        functionName: `${envPrefix}open-webui-gw-metering-interceptor`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.lambda_handler',
        code: lambda.Code.fromAsset(stagingDir),
        timeout: cdk.Duration.seconds(10),
        memorySize: 256,
        environment: {
          TARGET_PREFIX: 'bedrock/',
          TABLE: `${envPrefix}open-webui-metering`,
          JWKS_URL: `https://cognito-idp.${cdk.Aws.REGION}.amazonaws.com/${props.userPool.userPoolId}/.well-known/jwks.json`,
          ENFORCE_MODE: this.node.tryGetContext('meteringMode') === 'observe' ? 'OBSERVE' : 'ENFORCE',
          MAX_TOKENS_CLAMP: '8192',
          RPM_LIMIT_DEFAULT: '30',
          GRACE_REQUESTS: '10',
          HARD_DEFAULT_USD: '5',
          SOFT_DEFAULT_USD: '4',
          GROUP_ORDER: JSON.stringify(
            JSON.parse(
              fs.readFileSync(path.join(__dirname, '..', '..', 'config', 'metering-groups.json'), 'utf-8'),
            ).groups ?? [],
          ),
          // group → project-id map from the provisioner above; the interceptor
          // injects OpenAI-Project / anthropic-workspace-id per request.
          PROJECT_MAP: projects.getAttString('ProjectMapJson'),
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      // Least-privilege: read policies/counters + write estimates/RPM ticks.
      interceptor.addToRolePolicy(new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:BatchGetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem'],
        resources: [
          `arn:aws:dynamodb:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:table/${envPrefix}open-webui-metering`,
        ],
      }));
      interceptor.addToRolePolicy(new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'Metering' } },
      }));

      const version = interceptor.currentVersion;
      const alias = new lambda.Alias(this, 'MeteringInterceptorLive', {
        aliasName: 'live',
        version,
      });
      new codedeploy.LambdaDeploymentGroup(this, 'MeteringInterceptorCanary', {
        alias,
        deploymentConfig: codedeploy.LambdaDeploymentConfig.CANARY_10PERCENT_5MINUTES,
        alarms: [
          interceptor.metricErrors({ period: cdk.Duration.minutes(1) }).createAlarm(this, 'InterceptorErrAlarm', {
            threshold: 3,
            evaluationPeriods: 2,
            treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
          }),
        ],
      });
      interceptorInvokeArn = alias.functionArn;
      this.meteringInterceptorAlias = alias;
      this.meteringInterceptorFn = interceptor;
    } else {
      interceptor = new lambda.Function(this, 'ModelsFilter', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.lambda_handler',
        code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'gateway', 'interceptor')),
        timeout: cdk.Duration.seconds(10),
        memorySize: 128,
        environment: {
          MODEL_CAPS: JSON.stringify(modelCaps),
          TARGET_PREFIX: 'bedrock/',
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });
      interceptorInvokeArn = interceptor.functionArn;
    }

    // ── Gateway execution role (outbound to bedrock-mantle + invoke interceptor) ──
    const gatewayRole = new iam.Role(this, 'GatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': cdk.Aws.ACCOUNT_ID } },
      }),
    });
    // bedrock-mantle is its own IAM service prefix (CallWithBearerToken etc.);
    // plain bedrock:* is NOT sufficient for the OpenAI-compatible endpoint.
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-mantle:*'],
      resources: ['*'],
    }));
    if (this.meteringInterceptorAlias) {
      this.meteringInterceptorAlias.grantInvoke(gatewayRole);
    } else {
      interceptor.grantInvoke(gatewayRole);
    }

    // ── The gateway (native CFN resource) ──────────────────────────────────
    const gateway = new cdk.CfnResource(this, 'InferenceGateway', {
      type: 'AWS::BedrockAgentCore::Gateway',
      properties: {
        Name: `${envPrefix}open-webui-models`,
        RoleArn: gatewayRole.roleArn,
        ProtocolType: 'MCP',
        AuthorizerType: 'CUSTOM_JWT',
        AuthorizerConfiguration: {
          CustomJWTAuthorizer: {
            DiscoveryUrl: discoveryUrl,
            AllowedClients: this.canaryClient
              ? [props.userPoolClient.userPoolClientId, this.canaryClient.userPoolClientId]
              : [props.userPoolClient.userPoolClientId],
          },
        },
        InterceptorConfigurations: [
          {
            Interceptor: { Lambda: { Arn: interceptorInvokeArn } },
            InterceptionPoints: ['REQUEST'],
            InputConfiguration: { PassRequestHeaders: true },
          },
        ],
      },
    });
    this.gatewayId = gateway.getAtt('GatewayIdentifier').toString();
    this.gatewayUrl = gateway.getAtt('GatewayUrl').toString();

    // ── Inference target (bedrock-mantle connector) via custom resource ────
    const provisioner = new lambda.Function(this, 'TargetProvisioner', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'gateway', 'provisioner')),
      timeout: cdk.Duration.minutes(6),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
    });
    provisioner.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:CreateGatewayTarget',
        'bedrock-agentcore:DeleteGatewayTarget',
        'bedrock-agentcore:GetGatewayTarget',
        'bedrock-agentcore:ListGatewayTargets',
      ],
      // Scoped to THIS gateway (create/list authorize against the gateway
      // resource; get/delete against its targets) — a wildcard here would let
      // the provisioner mutate targets on any gateway in the account
      // (security scan 2026-08-21, IAM and Authorization).
      resources: [
        `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:gateway/${this.gatewayId}`,
        `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:gateway/${this.gatewayId}/target/*`,
      ],
    }));

    const targetProvider = new cr.Provider(this, 'TargetProvider', {
      onEventHandler: provisioner,
    });
    const target = new cdk.CustomResource(this, 'MantleTarget', {
      serviceToken: targetProvider.serviceToken,
      properties: {
        GatewayIdentifier: this.gatewayId,
        TargetName: 'bedrock',
        ConnectorId: 'bedrock-mantle',
      },
    });
    target.node.addDependency(gateway);

    // ── Opt-in: scheduled model-capability refresher ───────────────────────
    // Bedrock Mantle adds/moves models over time. When enabled, a scheduled
    // Lambda re-probes the catalog and refreshes the interceptor's MODEL_CAPS
    // (and re-snapshots the connector so new models route, not just list),
    // with a collapse-guard + SNS diff. DEFAULT OFF: the base sample is
    // unchanged unless `-c enableModelRefresh=true` is passed.
    const enableRefresh =
      this.node.tryGetContext('enableModelRefresh') === true ||
      this.node.tryGetContext('enableModelRefresh') === 'true';

    if (enableRefresh) {
      const refreshRateHours = Number(this.node.tryGetContext('modelRefreshRateHours') ?? 24);

      // Optional SNS topic for diff/alert notifications.
      const alertTopic = new sns.Topic(this, 'ModelRefreshAlerts', {
        displayName: `${envPrefix}open-webui model-refresh`,
      });

      const refresher = new lambda.Function(this, 'ModelRefresher', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.handler',
        code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'gateway', 'refresher')),
        // The probe issues up to 5 signed calls per model across the catalog;
        // give it room but bound it.
        timeout: cdk.Duration.minutes(10),
        memorySize: 256,
        environment: {
          INTERCEPTOR_FUNCTION_NAME: interceptor.functionName,
          GATEWAY_ID: this.gatewayId,
          CONNECTOR_TARGET_NAME: 'bedrock',
          CONNECTOR_ID: 'bedrock-mantle',
          MANTLE_REGION: mantleRegion,
          SNS_TOPIC_ARN: alertTopic.topicArn,
          COLLAPSE_MIN_RATIO: '0.5',
          // Metering module: the gateway invokes the interceptor via a "live"
          // alias pinned to a published (frozen-env) version. Updating $LATEST
          // alone is invisible to traffic, so the refresher must publish a new
          // version and repoint the alias. Absent (base sample) ⇒ gateway hits
          // $LATEST directly and updating it is sufficient.
          ...(this.meteringInterceptorAlias ? { INTERCEPTOR_ALIAS: this.meteringInterceptorAlias.aliasName } : {}),
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });

      // Probe bedrock-mantle (its own IAM service prefix).
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:*'],
        resources: ['*'],
      }));
      // Update the interceptor's MODEL_CAPS (read to diff + write to apply). The
      // function_updated_v2 waiter polls GetFunction; the alias-qualified read
      // (GetFunctionConfiguration on :live) and publish-version + repoint-alias
      // promotion need function + alias scope.
      const interceptorArns = [interceptor.functionArn, `${interceptor.functionArn}:*`];
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: [
          'lambda:GetFunction',
          'lambda:GetFunctionConfiguration',
          'lambda:UpdateFunctionConfiguration',
          'lambda:PublishVersion',
          'lambda:UpdateAlias',
        ],
        resources: interceptorArns,
      }));
      // Re-snapshot the connector target (list to find it, update to refresh).
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:ListGatewayTargets',
          'bedrock-agentcore:GetGatewayTarget',
          'bedrock-agentcore:UpdateGatewayTarget',
        ],
        // Scoped to THIS gateway and its targets — the refresher must never be
        // able to update targets on other gateways in the account
        // (security scan 2026-08-21, IAM and Authorization).
        resources: [
          `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:gateway/${this.gatewayId}`,
          `arn:${cdk.Aws.PARTITION}:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:gateway/${this.gatewayId}/target/*`,
        ],
      }));
      alertTopic.grantPublish(refresher);

      // EventBridge Scheduler → invoke the refresher on a fixed cadence.
      const schedulerRole = new iam.Role(this, 'ModelRefreshSchedulerRole', {
        assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
      });
      refresher.grantInvoke(schedulerRole);

      new scheduler.CfnSchedule(this, 'ModelRefreshSchedule', {
        flexibleTimeWindow: { mode: 'FLEXIBLE', maximumWindowInMinutes: 15 },
        scheduleExpression: `rate(${refreshRateHours} hours)`,
        target: {
          arn: refresher.functionArn,
          roleArn: schedulerRole.roleArn,
        },
        description: 'Refresh gateway model capabilities from live bedrock-mantle (opt-in).',
      });

      new cdk.CfnOutput(this, 'ModelRefreshTopicArn', {
        value: alertTopic.topicArn,
        description: 'Subscribe to receive model-refresh diffs and collapse-guard alerts.',
      });
    }

    // ── Outputs (consumed by the compute stack's seeder + deploy.sh) ───────
    new cdk.CfnOutput(this, 'GatewayId', { value: this.gatewayId });
    new cdk.CfnOutput(this, 'GatewayUrl', { value: this.gatewayUrl });
    // The GatewayUrl attribute is the MCP endpoint (…/mcp). The inference
    // endpoint is …/inference on the same host, so build it from the id.
    this.gatewayInferenceUrl =
      `https://${this.gatewayId}.gateway.bedrock-agentcore.${cdk.Aws.REGION}.amazonaws.com/inference`;
    new cdk.CfnOutput(this, 'GatewayInferenceUrl', {
      value: this.gatewayInferenceUrl,
      description: 'Base URL for Open WebUI OpenAI connections (system_oauth) and the Claude pipe.',
    });
    new cdk.CfnOutput(this, 'MantleRegion', { value: mantleRegion });
  }
}
