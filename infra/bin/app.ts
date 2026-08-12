#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import * as fs from 'fs';
import * as path from 'path';
import { NetworkStack } from '../lib/network-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { GatewayStack } from '../lib/gateway-stack';
import { ComputeStack } from '../lib/compute-stack';
import { MeteringStack } from '../lib/metering-stack';
import { EnvironmentConfig, getDevConfig, getProdConfig } from '../lib/environment-config';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
};

// Load persistent deployment config (written by deploy.sh)
// CLI context overrides file values, file values override defaults
const configPath = path.join(__dirname, '..', 'deploy.config.json');
let fileConfig: Record<string, string> = {};
if (fs.existsSync(configPath)) {
  fileConfig = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
}

function getConfig(key: string): string | undefined {
  return app.node.tryGetContext(key) || fileConfig[key] || undefined;
}

// Environment-aware configuration
const environment = app.node.tryGetContext('environment') as string | undefined;

// Opt-in metering module (docs/METERING.md). OFF unless -c metering=on
// (deploy.sh --metering). When off, nothing below references it and the base
// five stacks are unaffected.
const meteringEnabled = (getConfig('metering') ?? 'off') === 'on';

let envConfig: EnvironmentConfig | undefined;
if (environment === 'dev') {
  envConfig = getDevConfig();
} else if (environment === 'prod') {
  envConfig = getProdConfig();
}

// Stack name prefix: "OpenWebUI-Dev-" / "OpenWebUI-Prod-" / "OpenWebUI-" (default)
const prefix = envConfig ? `${envConfig.stackPrefix}-` : 'OpenWebUI-';

// Domain/cert: from config factory, then env-scoped config, then generic config
const envKey = envConfig?.environment; // "dev" | "prod" | undefined
const domainName = envConfig?.domainName
  ?? (envKey ? getConfig(`${envKey}DomainName`) : undefined)
  ?? getConfig('domainName');
const certificateArn = envConfig?.certificateArn
  ?? (envKey ? getConfig(`${envKey}CertificateArn`) : undefined)
  ?? getConfig('certificateArn');
const appUrl = domainName ? `https://${domainName}` : undefined;

// Fallback app URL for environments without a custom domain (e.g., dev using CloudFront URL)
// Used for Cognito callback URLs where the URL must be known at AuthStack deploy time
const devUrl = getConfig('devUrl');
const effectiveAppUrl = appUrl ?? (devUrl ? `https://${devUrl}` : undefined);

// Cognito domain prefix — must be unique per environment
const cognitoDomainPrefix = envConfig
  ? `open-webui-${envConfig.environment}-${process.env.CDK_DEFAULT_ACCOUNT}`
  : `open-webui-${process.env.CDK_DEFAULT_ACCOUNT}`;

// Network Stack
const networkStack = new NetworkStack(app, `${prefix}Network`, { env });

// Data Stack
const dataStack = new DataStack(app, `${prefix}Data`, {
  env,
  vpc: networkStack.vpc,
  ecsSecurityGroup: networkStack.ecsSecurityGroup,
  auroraMinCapacity: envConfig?.auroraMinCapacity,
  auroraMaxCapacity: envConfig?.auroraMaxCapacity,
  auroraDeletionProtection: envConfig?.auroraDeletionProtection,
});
dataStack.addDependency(networkStack);

// Auth Stack
const authStack = new AuthStack(app, `${prefix}Auth`, {
  env,
  callbackUrls: effectiveAppUrl
    ? [`${effectiveAppUrl}/oauth/oidc/callback`]
    : ['https://localhost/oauth/oidc/callback'],
  logoutUrls: effectiveAppUrl
    ? [`${effectiveAppUrl}/auth`]
    : ['https://localhost/auth'],
  cognitoDomainPrefix,
});

// Gateway Stack — AgentCore inference gateway fronting bedrock-mantle, trusting
// the Auth stack's Cognito pool for per-user (system_oauth) model access.
const gatewayStack = new GatewayStack(app, `${prefix}Gateway`, {
  env,
  userPool: authStack.userPool,
  userPoolClient: authStack.userPoolClient,
  environmentPrefix: envConfig?.environment,
  metering: meteringEnabled,
});
gatewayStack.addDependency(authStack);

// Metering Stack — opt-in; synthesized only when the flag is on. Created
// before Compute so the compute stack can wire the seeded filter to the
// metering bus/table.
let meteringStack: MeteringStack | undefined;
if (meteringEnabled) {
  meteringStack = new MeteringStack(app, `${prefix}Metering`, {
    env,
    userPool: authStack.userPool,
    userPoolClient: authStack.userPoolClient,
    canaryClient: gatewayStack.canaryClient!,
    gatewayId: gatewayStack.gatewayId,
    gatewayInferenceUrl: gatewayStack.gatewayInferenceUrl,
    userPoolDomainName: `${cognitoDomainPrefix}.auth.${env.region}.amazoncognito.com`,
    environmentPrefix: envConfig?.environment,
  });
  meteringStack.addDependency(gatewayStack);
}

// Open WebUI container image — configurable via -c openWebuiImage=…
const openWebuiImage = getConfig('openWebuiImage');

// Compute Stack
const computeStack = new ComputeStack(app, `${prefix}Compute`, {
  env,
  vpc: networkStack.vpc,
  ecsSecurityGroup: networkStack.ecsSecurityGroup,
  albSecurityGroup: networkStack.albSecurityGroup,
  auroraCluster: dataStack.auroraCluster,
  uploadBucket: dataStack.uploadBucket,
  redisEndpoint: dataStack.redisEndpoint,
  userPool: authStack.userPool,
  userPoolClient: authStack.userPoolClient,
  userPoolDomainName: `${cognitoDomainPrefix}.auth.${env.region}.amazoncognito.com`,
  domainName,
  certificateArn,
  openWebuiImage,
  ecsDesiredCount: envConfig?.ecsDesiredCount,
  ecsMinCapacity: envConfig?.ecsMinCapacity,
  ecsMaxCapacity: envConfig?.ecsMaxCapacity,
  enableAutoScaling: envConfig?.enableAutoScaling,
  environmentPrefix: envConfig?.environment,
  gatewayInferenceUrl: gatewayStack.gatewayInferenceUrl,
  metering: meteringStack
    ? { busName: meteringStack.bus.eventBusName, tableName: meteringStack.table.tableName }
    : undefined,
});
computeStack.addDependency(dataStack);
computeStack.addDependency(authStack);
computeStack.addDependency(gatewayStack);
if (meteringStack) {
  computeStack.addDependency(meteringStack);
}

app.synth();
