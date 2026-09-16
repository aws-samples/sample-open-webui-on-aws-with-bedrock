import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { execFileSync } from 'child_process';
import { createHash } from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createExternalSearchStack } from '../bin/external-search';
import { MODULE_ROOT, searchAsset } from '../lib/asset';
import { ExternalSearchStack, SEARCH_REGIONS } from '../lib/external-search-stack';

let assetPath: string;
const temporaryDirectories: string[] = [];
const env = { account: '123456789012', region: 'us-east-1' };

function temporaryDirectory(): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'external-search-test-'));
  temporaryDirectories.push(directory);
  return directory;
}

function sourceFiles(directory: string, prefix = ''): string[] {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const relative = path.join(prefix, entry.name);
    if (entry.isDirectory() && entry.name !== '__pycache__') {
      return sourceFiles(path.join(directory, entry.name), relative);
    }
    return entry.isFile() && entry.name.endsWith('.py') ? [relative] : [];
  });
}

function writeManifest(directory: string): void {
  const filenames = ['requirements.txt', 'source-commit.txt', ...['runtime', 'provisioner']
    .flatMap(name => sourceFiles(path.join(directory, name), name))].sort();
  fs.writeFileSync(path.join(directory, 'source-sha256.txt'), filenames.map(filename =>
    `${createHash('sha256').update(fs.readFileSync(path.join(directory, filename))).digest('hex')}  ${filename}\n`).join(''));
}

function testCases<Value>(values: readonly Value[]): (name: string, run: (value: Value) => void) => void {
  return (name, run) => {
    for (const value of values) test(name.replace('%s', String(value)), () => run(value));
  };
}

before(() => {
  assetPath = temporaryDirectory();
  for (const directory of ['runtime', 'provisioner']) {
    fs.cpSync(path.join(MODULE_ROOT, directory), path.join(assetPath, directory), { recursive: true });
  }
  fs.copyFileSync(path.join(MODULE_ROOT, 'requirements.txt'), path.join(assetPath, 'requirements.txt'));
  fs.writeFileSync(path.join(assetPath, 'source-commit.txt'), execFileSync('git', ['rev-parse', 'HEAD'], { cwd: MODULE_ROOT }));
  writeManifest(assetPath);
  for (const packageName of ['boto3', 'botocore', 's3transfer', 'jmespath', 'dateutil', 'urllib3', 'httpx', 'jsonschema']) {
    fs.mkdirSync(path.join(assetPath, packageName));
    fs.writeFileSync(path.join(assetPath, packageName, '__init__.py'), '__version__ = "1.43.94"\n');
  }
  for (const packageName of ['boto3', 'botocore']) {
    const version = fs.readFileSync(path.join(MODULE_ROOT, 'requirements.txt'), 'utf8')
      .match(new RegExp(`^${packageName}==([0-9.]+)`, 'm'))![1];
    fs.writeFileSync(path.join(assetPath, packageName, '__init__.py'), `__version__ = "${version}"\n`);
    fs.mkdirSync(path.join(assetPath, `${packageName}-${version}.dist-info`));
    fs.writeFileSync(path.join(assetPath, `${packageName}-${version}.dist-info`, 'METADATA'), `\nVersion: ${version}\n`);
  }
  fs.mkdirSync(path.join(assetPath, 'botocore/data/bedrock-agentcore-control'), { recursive: true });
  fs.writeFileSync(path.join(assetPath, 'six.py'), '');
});

after(() => {
  for (const directory of temporaryDirectories) fs.rmSync(directory, { recursive: true, force: true });
});

function stack(environment: cdk.Environment = env, gatewayName?: string): ExternalSearchStack {
  return new ExternalSearchStack(new cdk.App({ outdir: temporaryDirectory() }), 'OpenWebUI-ExternalSearch', {
    env: environment, assetPath, gatewayName,
  });
}

function roleStatements(resources: Record<string, any>, roleId: string): any[] {
  return Object.values(resources).filter(resource => resource.Type === 'AWS::IAM::Policy'
    && resource.Properties.Roles.some((role: any) => role.Ref === roleId))
    .flatMap(resource => resource.Properties.PolicyDocument.Statement);
}

test('standalone app is the opt-in and never loads the parent application', () => {
  const app = new cdk.App({ outdir: temporaryDirectory(), context: { ...env, assetPath } });
  const deployedStack = createExternalSearchStack(app);
  assert.equal(deployedStack.stackName, 'OpenWebUI-ExternalSearch');
  assert.deepEqual(app.node.children, [deployedStack]);
  assert.equal((app.synth().stacks).length, 1);
  const template = Template.fromStack(deployedStack).toJSON();
  const allowed = new Set(['AWS::BedrockAgentCore::Gateway', 'AWS::BedrockAgentCore::GatewayTarget',
    'Custom::WebSearchVersionPin', 'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::Logs::LogGroup',
    'AWS::SecretsManager::Secret', 'AWS::Lambda::Function', 'AWS::Lambda::Version',
    'AWS::Lambda::Alias', 'AWS::Lambda::Url', 'AWS::Lambda::Permission']);
  for (const resource of Object.values(template.Resources) as any[]) assert.equal(allowed.has(resource.Type), true);
  assert.doesNotMatch(JSON.stringify(template), /Fn::ImportValue|Browser|DynamoDB|Cognito|InvokeModel|inference|VpcConfig|daily.?quota/i);
  assert.equal((fs.readFileSync(path.join(MODULE_ROOT, 'cdk.json'), 'utf8')).includes('infra/'), false);
});

testCases(SEARCH_REGIONS)('accepts explicitly configured search region %s', region => {
  assert.doesNotThrow(() => Template.fromStack(stack({ ...env, region })));
});

testCases([undefined, '', 'us-west-2', 'eu-central-1'])('rejects missing or unsupported region %s', region => {
  assert.throws(() => stack({ account: env.account, region }), /explicit supported region/);
});

testCases([undefined, '', '123', 'abcdefghijkl'])('rejects missing or malformed account %s', account => {
  assert.throws(() => stack({ account, region: env.region }), /explicit 12-digit account/);
});

test('CLI context cannot silently fall back to ambient CDK account/region', () => {
  const previousAccount = process.env.CDK_DEFAULT_ACCOUNT;
  const previousRegion = process.env.CDK_DEFAULT_REGION;
  process.env.CDK_DEFAULT_ACCOUNT = env.account;
  process.env.CDK_DEFAULT_REGION = env.region;
  try {
    assert.throws(() => createExternalSearchStack(new cdk.App({ context: { assetPath } })), /explicit 12-digit account/);
    assert.throws(() => createExternalSearchStack(new cdk.App({ context: { assetPath, account: env.account } })), /explicit supported region/);
  } finally {
    if (previousAccount === undefined) delete process.env.CDK_DEFAULT_ACCOUNT;
    else process.env.CDK_DEFAULT_ACCOUNT = previousAccount;
    if (previousRegion === undefined) delete process.env.CDK_DEFAULT_REGION;
    else process.env.CDK_DEFAULT_REGION = previousRegion;
  }
});

test('native IAM MCP gateway and single connector target use a bounded version pin', () => {
  const template = Template.fromStack(stack());
  template.resourceCountIs('AWS::BedrockAgentCore::Gateway', 1);
  template.resourceCountIs('AWS::BedrockAgentCore::GatewayTarget', 1);
  template.hasResourceProperties('AWS::BedrockAgentCore::Gateway', {
    AuthorizerType: 'AWS_IAM', ProtocolType: 'MCP',
    ProtocolConfiguration: { Mcp: { SupportedVersions: ['2025-03-26'] } },
    AuthorizerConfiguration: Match.absent(), ExceptionLevel: Match.absent(),
  });
  template.hasResourceProperties('AWS::BedrockAgentCore::GatewayTarget', {
    Name: 'web-search-tool', GatewayIdentifier: { 'Fn::GetAtt': ['SearchGateway', 'GatewayIdentifier'] },
    TargetConfiguration: { Mcp: { Connector: {
      Source: { ConnectorId: 'web-search' }, Enabled: ['WebSearch'],
      Configurations: [{ Name: 'WebSearch', ParameterValues: {} }],
    } } },
    CredentialProviderConfigurations: [{ CredentialProviderType: 'GATEWAY_IAM_ROLE' }],
  });
  template.hasResourceProperties('Custom::WebSearchVersionPin', {
    TargetId: { 'Fn::GetAtt': ['SearchTarget', 'TargetId'] }, TargetName: 'web-search-tool',
    ConnectorId: 'web-search', ConnectorVersion: '1.2.0',
  });
  const resources = template.toJSON().Resources;
  assert.equal(resources.SearchTarget.DeletionPolicy, 'Delete');
  assert.equal(resources.SearchTarget.UpdateReplacePolicy, 'Delete');
  assert.ok((resources.SearchVersionPin.DependsOn).includes('SearchTarget'));
  assert.equal((resources.SearchTarget.DependsOn).includes('SearchVersionPin'), false);
  const adapter = Object.values(resources).find((resource: any) => resource.Properties?.Handler === 'runtime.handler.handler') as any;
  assert.ok((adapter.DependsOn).includes('SearchVersionPin'));
});

test('IAM separates adapter, gateway and provisioner with exact owned resources', () => {
  const resources = Template.fromStack(stack()).toJSON().Resources as Record<string, any>;
  const gatewayRoleId = resources.SearchGateway.Properties.RoleArn['Fn::GetAtt'][0];
  const gatewayRole = resources[gatewayRoleId];
  assert.deepEqual(gatewayRole.Properties.AssumeRolePolicyDocument.Statement[0].Condition, {
    StringEquals: { 'aws:SourceAccount': env.account },
    ArnLike: { 'aws:SourceArn': { 'Fn::Join': ['', [
      'arn:', { Ref: 'AWS::Partition' }, ':bedrock-agentcore:us-east-1:123456789012:gateway/*',
    ]] } },
  });
  assert.deepEqual(gatewayRole.Properties.Policies[0].PolicyDocument.Statement, [{
    Effect: 'Allow', Action: 'bedrock-agentcore:InvokeWebSearch',
    Resource: { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' }, ':bedrock-agentcore:us-east-1:aws:tool/web-search.v1']] },
  }]);
  const exactGateway = { 'Fn::GetAtt': ['SearchGateway', 'GatewayArn'] };
  assert.deepEqual(roleStatements(resources, gatewayRoleId), [{
    Effect: 'Allow', Action: 'bedrock-agentcore:InvokeGateway', Resource: exactGateway,
  }]);
  const adapter = Object.values(resources).find(resource => resource.Properties?.Handler === 'runtime.handler.handler');
  const adapterStatements = roleStatements(resources, adapter.Properties.Role['Fn::GetAtt'][0])
    .filter(statement => !JSON.stringify(statement.Action).includes('logs:'));
  assert.deepEqual(adapterStatements, [
    { Effect: 'Allow', Action: 'secretsmanager:GetSecretValue', Resource: adapter.Properties.Environment.Variables.SERVICE_SECRET_ARN },
    { Effect: 'Allow', Action: 'bedrock-agentcore:InvokeGateway', Resource: exactGateway },
  ]);
  const provisioner = Object.values(resources).find(resource => resource.Properties?.Handler === 'provisioner.index.handler');
  const provisionerStatements = roleStatements(resources, provisioner.Properties.Role['Fn::GetAtt'][0])
    .filter(statement => !JSON.stringify(statement.Action).includes('logs:'));
  assert.deepEqual(provisionerStatements, [{
    Effect: 'Allow', Action: ['bedrock-agentcore:GetGatewayTarget', 'bedrock-agentcore:UpdateGatewayTarget',
      'bedrock-agentcore:SynchronizeGatewayTargets'], Resource: exactGateway,
  }]);
  for (const resource of Object.values(resources)) {
    if (resource.Type === 'AWS::IAM::Role') assert.equal(resource.Properties.ManagedPolicyArns, undefined);
    if (resource.Type === 'AWS::IAM::Policy') {
      for (const statement of resource.Properties.PolicyDocument.Statement) assert.notEqual(statement.Resource, '*');
    }
  }
  assert.doesNotMatch(JSON.stringify(resources), /iam:PassRole|bedrock-agentcore:\*|DeleteGatewayTarget|CreateGatewayTarget|ListGatewayTargets/);
});

test('bearer-authenticated public URL is alias-qualified with constrained runtime and generated secret', () => {
  const template = Template.fromStack(stack());
  template.resourceCountIs('AWS::Lambda::Function', 3);
  template.resourceCountIs('AWS::Lambda::Url', 1);
  template.resourceCountIs('AWS::Lambda::Alias', 1);
  template.resourceCountIs('AWS::SecretsManager::Secret', 1);
  template.hasResourceProperties('AWS::SecretsManager::Secret', {
    GenerateSecretString: { PasswordLength: 64, ExcludePunctuation: true, IncludeSpace: false },
    SecretString: Match.absent(),
  });
  template.hasResourceProperties('AWS::Lambda::Function', {
    Runtime: 'python3.12', Architectures: ['arm64'], Handler: 'runtime.handler.handler',
    Timeout: 30, MemorySize: 256, ReservedConcurrentExecutions: 5,
    Environment: { Variables: {
      GATEWAY_URL: { 'Fn::GetAtt': ['SearchGateway', 'GatewayUrl'] }, GATEWAY_TARGET_NAME: 'web-search-tool',
      SERVICE_SECRET_ARN: Match.anyValue(), SEARCH_REGION: 'us-east-1',
    } },
  });
  template.hasResourceProperties('AWS::Lambda::Function', {
    Runtime: 'python3.12', Architectures: ['arm64'], Handler: 'provisioner.index.handler', Timeout: 360,
  });
  template.hasResourceProperties('AWS::Lambda::Function', {
    LoggingConfig: Match.objectLike({ ApplicationLogLevel: 'FATAL', LogFormat: 'JSON' }),
  });
  template.hasResourceProperties('AWS::Lambda::Alias', { Name: 'live' });
  template.hasResourceProperties('AWS::Lambda::Url', { AuthType: 'NONE', Qualifier: 'live', Cors: Match.absent() });
  template.hasResourceProperties('AWS::Lambda::Permission', {
    Principal: '*', Action: 'lambda:InvokeFunctionUrl', FunctionUrlAuthType: 'NONE',
  });
  template.hasResourceProperties('AWS::Lambda::Permission', {
    Principal: '*', Action: 'lambda:InvokeFunction', InvokedViaFunctionUrl: true,
  });
  const resources = template.toJSON().Resources as Record<string, any>;
  const pythonFunctions = Object.values(resources).filter(resource => resource.Properties?.Runtime === 'python3.12');
  assert.deepEqual(pythonFunctions[0].Properties.Code, pythonFunctions[1].Properties.Code);
  for (const resource of Object.values(resources).filter(resource => resource.Type === 'AWS::Logs::LogGroup')) {
    assert.equal(resource.Properties.RetentionInDays, 7);
    assert.equal(resource.DeletionPolicy, 'Delete');
  }
  const secret = Object.values(resources).find(resource => resource.Type === 'AWS::SecretsManager::Secret');
  assert.equal(secret.DeletionPolicy, 'Retain');
  const outputs = template.toJSON().Outputs;
  assert.deepEqual(Object.keys(outputs).sort(), ['SearchURL', 'ApiKeySecretArn', 'AdapterArn', 'GatewayId', 'GatewayUrl', 'ConnectorVersion'].sort());
  assert.equal(outputs.SearchURL.Value['Fn::Join'][1][1], 'search');
  assert.equal(outputs.ConnectorVersion.Value, '1.2.0');
  assert.equal((JSON.stringify(outputs)).includes('secretsmanager:'), false);
});

test('default gateway name includes stack identity; explicit safe names are supported', () => {
  const defaultName = Template.fromStack(stack()).toJSON().Resources.SearchGateway.Properties.Name;
  assert.ok((JSON.stringify(defaultName)).includes('search-openwebu-'));
  assert.ok((JSON.stringify(defaultName)).includes('AWS::StackId'));
  const resolvedName = defaultName['Fn::Join'][1][0] + 'f4930a20-b1da-11f1-91f6-0affdffb0835';
  assert.match(resolvedName, /^([0-9a-zA-Z][-]?){1,48}$/);
  assert.equal(Template.fromStack(stack(env, 'my-private-search')).toJSON().Resources.SearchGateway.Properties.Name, 'my-private-search');
  assert.throws(() => stack(env, 'invalid/name'), /gatewayName/);
  assert.throws(() => stack(env, 'a'.repeat(49)), /gatewayName/);
  assert.throws(() => stack(env, 'invalid--name'), /gatewayName/);
});

testCases(['', 'relative/bundle', '/does/not/exist'])('asset path %s must be a prebuilt absolute directory', asset => {
  assert.throws(() => searchAsset(asset), /absolute directory/);
});

testCases(['runtime/handler.py', 'provisioner/index.py', 'requirements.txt', 'source-commit.txt', 'source-sha256.txt',
  'boto3/__init__.py', 'botocore/__init__.py', 'urllib3/__init__.py', 'httpx/__init__.py', 'jsonschema/__init__.py', 'six.py',
  'botocore/data/bedrock-agentcore-control'])('rejects incomplete bundle without %s', filename => {
  const bundle = temporaryDirectory();
  fs.cpSync(assetPath, bundle, { recursive: true });
  fs.rmSync(path.join(bundle, filename), { recursive: true, force: true });
  assert.throws(() => searchAsset(bundle));
});

testCases(['runtime/handler.py', 'provisioner/index.py', 'requirements.txt'])('rejects stale source even with a recomputed manifest: %s', filename => {
  const bundle = temporaryDirectory();
  fs.cpSync(assetPath, bundle, { recursive: true });
  fs.appendFileSync(path.join(bundle, filename), '\n');
  writeManifest(bundle);
  assert.throws(() => searchAsset(bundle), /stale/);
});

test('rejects wrong HEAD, forged manifest, and unexpected old Python source', () => {
  const bundle = temporaryDirectory();
  fs.cpSync(assetPath, bundle, { recursive: true });
  const marker = path.join(bundle, 'source-commit.txt');
  const original = fs.readFileSync(marker);
  fs.writeFileSync(marker, `${'0'.repeat(40)}\n`);
  writeManifest(bundle);
  assert.throws(() => searchAsset(bundle), /current HEAD/);
  fs.writeFileSync(marker, original);
  assert.throws(() => searchAsset(bundle), /source manifest/);
  writeManifest(bundle);
  fs.writeFileSync(path.join(bundle, 'runtime/old.py'), '');
  writeManifest(bundle);
  assert.throws(() => searchAsset(bundle), /source files differ/);
});

test('CDK schema still requires the narrowly scoped version-pin workaround', () => {
  const declaration = fs.readFileSync(path.join(path.dirname(require.resolve('aws-cdk-lib/package.json')),
    'aws-bedrockagentcore/lib/bedrockagentcore.generated.d.ts'), 'utf8');
  const connectorSource = declaration.match(/interface ConnectorSourceProperty \{([\s\S]*?)\n    \}/)?.[1];
  assert.ok(connectorSource);
  assert.ok((connectorSource).includes('readonly connectorId: string'));
  assert.doesNotMatch(connectorSource, /readonly version[?:]/);
});
