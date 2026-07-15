// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

export interface GatewayStackProps extends cdk.StackProps {
  userPool: cognito.UserPool;
  userPoolClient: cognito.UserPoolClient;
  /** Resource-name prefix (e.g. "dev", "prod"). Empty for the default single-env deploy. */
  environmentPrefix?: string;
  /** Region whose bedrock-mantle endpoint the gateway fronts (defaults to the stack region). */
  mantleRegion?: string;
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
 *  - A REQUEST interceptor Lambda filters the /v1/models listing to the
 *    capability-verified set (config/model-capabilities.json) per connection,
 *    so Open WebUI never surfaces a model that would fail on that API.
 *  - The bedrock-mantle inference *target* is created via a custom resource
 *    (no native CFN type for inference targets yet).
 */
export class GatewayStack extends cdk.Stack {
  public readonly gatewayUrl: string;
  public readonly gatewayId: string;
  public readonly gatewayInferenceUrl: string;

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

    // ── Interceptor Lambda: capability-filtered model listing ──────────────
    const interceptor = new lambda.Function(this, 'ModelsFilter', {
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
    interceptor.grantInvoke(gatewayRole);

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
            AllowedClients: [props.userPoolClient.userPoolClientId],
          },
        },
        InterceptorConfigurations: [
          {
            Interceptor: { Lambda: { Arn: interceptor.functionArn } },
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
      resources: ['*'],
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
        },
        logRetention: logs.RetentionDays.ONE_MONTH,
      });

      // Probe bedrock-mantle (its own IAM service prefix).
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:*'],
        resources: ['*'],
      }));
      // Update the interceptor's MODEL_CAPS (read to diff + write to apply).
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: ['lambda:GetFunctionConfiguration', 'lambda:UpdateFunctionConfiguration'],
        resources: [interceptor.functionArn],
      }));
      // Re-snapshot the connector target (list to find it, update to refresh).
      refresher.addToRolePolicy(new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:ListGatewayTargets',
          'bedrock-agentcore:GetGatewayTarget',
          'bedrock-agentcore:UpdateGatewayTarget',
        ],
        resources: ['*'],
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
