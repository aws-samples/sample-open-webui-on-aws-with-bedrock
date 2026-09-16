import * as cdk from 'aws-cdk-lib';
import { ExternalSearchStack } from '../lib/external-search-stack';

export function createExternalSearchStack(app: cdk.App): ExternalSearchStack {
  const stackName = app.node.tryGetContext('stackName') ?? 'OpenWebUI-ExternalSearch';
  if (typeof stackName !== 'string' || !/^[A-Za-z][A-Za-z0-9-]{0,127}$/.test(stackName)) {
    throw new Error('stackName must be a valid CloudFormation stack name');
  }
  return new ExternalSearchStack(app, stackName, {
    stackName,
    env: { account: app.node.tryGetContext('account'), region: app.node.tryGetContext('region') },
    assetPath: app.node.tryGetContext('assetPath'),
    gatewayName: app.node.tryGetContext('gatewayName'),
  });
}

if (require.main === module) {
  createExternalSearchStack(new cdk.App());
}
