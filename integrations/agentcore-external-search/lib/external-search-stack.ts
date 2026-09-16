import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { searchAsset } from './asset';

export const SEARCH_REGIONS = ['us-east-1', 'eu-west-1', 'ap-northeast-1'];
export const CONNECTOR_VERSION = '1.2.0';
export const TARGET_NAME = 'web-search-tool';

export interface ExternalSearchStackProps extends cdk.StackProps {
  readonly assetPath: string;
  readonly gatewayName?: string;
}

export class ExternalSearchStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ExternalSearchStackProps) {
    super(scope, id, props);
    const account = props.env?.account;
    const region = props.env?.region;
    if (typeof account !== 'string' || cdk.Token.isUnresolved(account) || !/^\d{12}$/.test(account)) {
      throw new Error('Search requires an explicit 12-digit account: -c account=...');
    }
    if (typeof region !== 'string' || cdk.Token.isUnresolved(region) || !SEARCH_REGIONS.includes(region)) {
      throw new Error(`Search requires an explicit supported region: ${SEARCH_REGIONS.join(', ')}`);
    }
    if (props.gatewayName !== undefined && (typeof props.gatewayName !== 'string'
      || !/^([0-9a-zA-Z][-]?){1,48}$/.test(props.gatewayName))) {
      throw new Error('gatewayName must match the Gateway schema: 1-48 alphanumeric characters, each optionally followed by one hyphen');
    }
    const code = searchAsset(props.assetPath);
    const logGroup = (name: string): logs.LogGroup => new logs.LogGroup(this, name, {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const executionRole = (name: string, group: logs.LogGroup): iam.Role => {
      const role = new iam.Role(this, name, { assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com') });
      group.grantWrite(role);
      return role;
    };
    const gatewayRole = new iam.Role(this, 'SearchGatewayRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': account },
          ArnLike: { 'aws:SourceArn': this.formatArn({ service: 'bedrock-agentcore', resource: 'gateway', resourceName: '*' }) },
        },
      }),
      inlinePolicies: {
        WebSearch: new iam.PolicyDocument({ statements: [new iam.PolicyStatement({
          actions: ['bedrock-agentcore:InvokeWebSearch'],
          resources: [this.formatArn({ service: 'bedrock-agentcore', account: 'aws', resource: 'tool', resourceName: 'web-search.v1' })],
        })] }),
      },
    });
    const suffix = cdk.Fn.select(2, cdk.Fn.split('/', this.stackId));
    const defaultName = `search-${this.stackName.toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 8) || 'owui'}-${suffix}`;
    const gateway = new agentcore.CfnGateway(this, 'SearchGateway', {
      name: props.gatewayName ?? defaultName,
      roleArn: gatewayRole.roleArn,
      protocolType: 'MCP',
      protocolConfiguration: { mcp: { supportedVersions: ['2025-03-26'] } },
      authorizerType: 'AWS_IAM',
    });
    const gatewayInvoke = new iam.Policy(this, 'GatewayInvoke', {
      roles: [gatewayRole],
      statements: [new iam.PolicyStatement({ actions: ['bedrock-agentcore:InvokeGateway'], resources: [gateway.attrGatewayArn] })],
    });
    const target = new agentcore.CfnGatewayTarget(this, 'SearchTarget', {
      gatewayIdentifier: gateway.attrGatewayIdentifier,
      name: TARGET_NAME,
      targetConfiguration: { mcp: { connector: {
        source: { connectorId: 'web-search' },
        enabled: ['WebSearch'],
        configurations: [{ name: 'WebSearch', parameterValues: {} }],
      } } },
      credentialProviderConfigurations: [{ credentialProviderType: 'GATEWAY_IAM_ROLE' }],
    });
    target.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
    target.node.addDependency(gatewayInvoke);
    const provisionerLogs = logGroup('ProvisionerLogs');
    const provisioner = new lambda.Function(this, 'SearchTargetProvisioner', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'provisioner.index.handler',
      code,
      timeout: cdk.Duration.minutes(6),
      memorySize: 256,
      logGroup: provisionerLogs,
      role: executionRole('ProvisionerRole', provisionerLogs),
    });
    provisioner.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:GetGatewayTarget', 'bedrock-agentcore:UpdateGatewayTarget',
        'bedrock-agentcore:SynchronizeGatewayTargets'],
      resources: [gateway.attrGatewayArn],
    }));
    const providerLogs = logGroup('ProviderLogs');
    const provider = new cr.Provider(this, 'SearchTargetProvider', {
      onEventHandler: provisioner,
      logGroup: providerLogs,
      frameworkOnEventRole: executionRole('ProviderRole', providerLogs),
      frameworkLambdaLoggingLevel: lambda.ApplicationLogLevel.FATAL,
    });
    const versionPin = new cdk.CustomResource(this, 'SearchVersionPin', {
      resourceType: 'Custom::WebSearchVersionPin',
      serviceToken: provider.serviceToken,
      properties: {
        GatewayIdentifier: gateway.attrGatewayIdentifier,
        TargetId: target.attrTargetId,
        TargetName: TARGET_NAME,
        ConnectorId: 'web-search',
        ConnectorVersion: CONNECTOR_VERSION,
      },
    });
    versionPin.node.addDependency(target, provisioner.role!);
    const secret = new secretsmanager.Secret(this, 'ApiKey', {
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      generateSecretString: { passwordLength: 64, excludePunctuation: true, includeSpace: false },
    });
    const adapterLogs = logGroup('AdapterLogs');
    const adapter = new lambda.Function(this, 'SearchAdapter', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'runtime.handler.handler',
      code,
      timeout: cdk.Duration.seconds(30),
      reservedConcurrentExecutions: 5,
      memorySize: 256,
      logGroup: adapterLogs,
      role: executionRole('AdapterRole', adapterLogs),
      environment: {
        GATEWAY_URL: gateway.attrGatewayUrl,
        GATEWAY_TARGET_NAME: TARGET_NAME,
        SERVICE_SECRET_ARN: secret.secretArn,
        SEARCH_REGION: region,
      },
    });
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'], resources: [secret.secretArn],
    }));
    adapter.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeGateway'], resources: [gateway.attrGatewayArn],
    }));
    adapter.node.addDependency(versionPin);
    const live = new lambda.Alias(this, 'Live', { aliasName: 'live', version: adapter.currentVersion });
    const url = live.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE });
    new cdk.CfnOutput(this, 'SearchURL', { value: cdk.Fn.join('', [url.url, 'search']) });
    new cdk.CfnOutput(this, 'ApiKeySecretArn', { value: secret.secretArn });
    new cdk.CfnOutput(this, 'AdapterArn', { value: live.functionArn });
    new cdk.CfnOutput(this, 'GatewayId', { value: gateway.attrGatewayIdentifier });
    new cdk.CfnOutput(this, 'GatewayUrl', { value: gateway.attrGatewayUrl });
    new cdk.CfnOutput(this, 'ConnectorVersion', { value: CONNECTOR_VERSION });
  }
}
