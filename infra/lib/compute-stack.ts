// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import { DockerImageAsset, Platform } from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as path from 'path';
import { Construct } from 'constructs';
import { Asset } from 'aws-cdk-lib/aws-s3-assets';

/**
 * Fallback Open WebUI image, used only when no override reaches the stack via
 * CDK context (-c openWebuiImage=…). The supported deploy path (./deploy.sh)
 * always passes that context: it resolves the .env OPEN_WEBUI_IMAGE selection —
 * or, when unset, the latest official release — to an immutable @sha256 digest
 * (scripts/resolve-owui-image.py), so this constant only matters for bare
 * `cdk deploy` runs. It points at a known release tag rather than :latest
 * because ghcr's :latest is upstream's main-branch build, not a release.
 *
 * The sample runs the upstream image completely unmodified — the Amazon
 * Bedrock integration is delivered at runtime as an Open WebUI pipe function +
 * OpenAI connections that point at the AgentCore inference gateway. No fork,
 * no patches, no image build.
 */
const DEFAULT_IMAGE = 'ghcr.io/open-webui/open-webui:v0.11.0';

export interface ComputeStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
  ecsSecurityGroup: ec2.SecurityGroup;
  albSecurityGroup: ec2.SecurityGroup;
  auroraCluster: rds.DatabaseCluster;
  uploadBucket: s3.Bucket;
  redisEndpoint: string;
  userPool: cognito.UserPool;
  userPoolClient: cognito.UserPoolClient;
  userPoolDomainName: string;
  /** Custom domain name (e.g., "oui.example.com"). If omitted, uses CloudFront default domain. */
  domainName?: string;
  /** ARN of an ACM certificate in us-east-1 for the custom domain. Required if domainName is set. */
  certificateArn?: string;
  /**
   * Open WebUI container image reference (tag or digest). Passed from .env via
   * CDK context (-c openWebuiImage=…). Falls back to DEFAULT_IMAGE when unset.
   */
  openWebuiImage?: string;
  cpu?: number;
  memoryLimitMiB?: number;
  ecsDesiredCount?: number;
  ecsMinCapacity?: number;
  ecsMaxCapacity?: number;
  enableAutoScaling?: boolean;
  /** Environment name for resource name prefixing (e.g., "dev", "prod"). Undefined for backward compat. */
  environmentPrefix?: string;
  /** AgentCore inference gateway base URL (…/inference), from the GatewayStack output. */
  gatewayInferenceUrl: string;
  /** Region whose bedrock-mantle endpoint backs the gateway (for the Claude pipe's discovery). */
  mantleRegion?: string;
  /**
   * Opt-in metering module wiring (docs/METERING.md). Absent (the default)
   * ⇒ nothing metering-related is added and the task definition is unchanged.
   */
  metering?: {
    /** EventBridge bus name the seeded filter publishes usage events to. */
    busName: string;
    /** Metering table name (the filter's read-only soft-warn snapshot). */
    tableName: string;
  };
}

export class ComputeStack extends cdk.Stack {
  public readonly ecsCluster: ecs.Cluster;
  public readonly fargateService: ecs.FargateService;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const {
      vpc, ecsSecurityGroup, albSecurityGroup, auroraCluster, uploadBucket,
      redisEndpoint, userPool, userPoolClient, userPoolDomainName,
    } = props;
    const cpu = props.cpu ?? 1024;
    const memoryLimitMiB = props.memoryLimitMiB ?? 2048;
    const envPrefix = props.environmentPrefix ? `${props.environmentPrefix}-` : '';
    const desiredCount = props.ecsDesiredCount ?? 1;
    const minCapacity = props.ecsMinCapacity ?? 1;
    const maxCapacity = props.ecsMaxCapacity ?? 10;
    const enableAutoScaling = props.enableAutoScaling ?? true;

    // =====================
    // Container Image — unmodified official Open WebUI, configurable via .env.
    // No Docker build anywhere; the Bedrock integration is seeded at runtime.
    // =====================
    const imageUri = props.openWebuiImage ?? DEFAULT_IMAGE;
    const containerImage = ecs.ContainerImage.fromRegistry(imageUri);
    const imageUriForOutput = imageUri;

    // =====================
    // Secrets Manager
    // =====================
    const webuiSecret = new secretsmanager.Secret(this, 'WebUISecretKey', {
      secretName: `open-webui/${envPrefix}webui-secret-key`,
      description: 'Secret key for Open WebUI JWT signing',
      generateSecretString: {
        secretStringTemplate: '{}',
        generateStringKey: 'WEBUI_SECRET_KEY',
        excludePunctuation: true,
        passwordLength: 64,
      },
    });

    const cognitoClientSecret = new secretsmanager.Secret(this, 'CognitoClientSecret', {
      secretName: `open-webui/${envPrefix}cognito-client-secret`,
      description: 'Cognito User Pool Client Secret',
    });

    // =====================
    // ECS Cluster
    // =====================
    this.ecsCluster = new ecs.Cluster(this, 'OpenWebUICluster', {
      vpc,
      clusterName: `${envPrefix}open-webui-cluster`,
      containerInsights: true,
    });

    // =====================
    // Task Role
    // =====================
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });

    // The Claude pipe discovers models by listing bedrock-mantle (SigV4, task
    // role) since Open WebUI's pipe discovery hook runs with no user context.
    // Actual inference goes through the gateway with the USER's OAuth token, so
    // the task role needs only the read/list surface here — not broad invoke.
    // bedrock-mantle is its own IAM service prefix (not plain bedrock:*).
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-mantle:Get*', 'bedrock-mantle:List*', 'bedrock-mantle:CallWithBearerToken'],
      resources: ['*'],
    }));
    uploadBucket.grantReadWrite(taskRole);
    webuiSecret.grantRead(taskRole);
    cognitoClientSecret.grantRead(taskRole);
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: ['*'],
    }));

    // =====================
    // Task Definition
    // =====================
    const taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDef', { cpu, memoryLimitMiB, taskRole });

    const logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: `/ecs/${envPrefix}open-webui`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Bedrock integration is delivered at container start: the seeder downloads
    // the Claude pipe + its installer (python + boto3 are already in the
    // official image) and runs it in the background. The installer waits for
    // DB migrations + the first admin sign-up, then upserts the pipe function
    // and the two OpenAI connections (chat_completions + responses) that point
    // at the gateway. Idempotent; never blocks or crashes app start.
    const pipeAsset = new Asset(this, 'ClaudePipeAsset', {
      path: path.join(__dirname, '..', '..', 'pipe', 'gateway_anthropic_pipe.py'),
    });
    const seedAsset = new Asset(this, 'SeedAsset', {
      path: path.join(__dirname, '..', '..', 'pipe', 'seed.py'),
    });
    pipeAsset.grantRead(taskRole);
    seedAsset.grantRead(taskRole);
    let pipeBootstrap =
      `(python -c "import boto3; s3 = boto3.client('s3'); ` +
      `s3.download_file('${pipeAsset.s3BucketName}', '${pipeAsset.s3ObjectKey}', '/tmp/gateway_anthropic_pipe.py'); ` +
      `s3.download_file('${seedAsset.s3BucketName}', '${seedAsset.s3ObjectKey}', '/tmp/seed.py')" && ` +
      `(python /tmp/seed.py >/proc/1/fd/1 2>&1 &)) ; `;

    // Opt-in metering module: deliver + seed the usage-metering filter the same
    // way (own assets + own seeder — pipe/seed.py stays byte-identical so the
    // off-state lean-core gate holds). Only reached when props.metering is set.
    if (props.metering) {
      const meterFilterAsset = new Asset(this, 'MeteringFilterAsset', {
        path: path.join(__dirname, '..', '..', 'pipe', 'metering_filter.py'),
      });
      const meterSeedAsset = new Asset(this, 'MeteringSeedAsset', {
        path: path.join(__dirname, '..', '..', 'pipe', 'metering_seed.py'),
      });
      meterFilterAsset.grantRead(taskRole);
      meterSeedAsset.grantRead(taskRole);
      pipeBootstrap +=
        `(python -c "import boto3; s3 = boto3.client('s3'); ` +
        `s3.download_file('${meterFilterAsset.s3BucketName}', '${meterFilterAsset.s3ObjectKey}', '/tmp/metering_filter.py'); ` +
        `s3.download_file('${meterSeedAsset.s3BucketName}', '${meterSeedAsset.s3ObjectKey}', '/tmp/metering_seed.py')" && ` +
        `(python /tmp/metering_seed.py >/proc/1/fd/1 2>&1 &)) ; `;
      // The filter publishes usage events and reads its own soft-warn snapshot.
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: ['events:PutEvents'],
        resources: [`arn:aws:events:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:event-bus/${props.metering.busName}`],
      }));
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: ['dynamodb:GetItem'],
        resources: [`arn:aws:dynamodb:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:table/${props.metering.tableName}`],
      }));
    }

    taskDefinition.addContainer('OpenWebUI', {
      image: containerImage,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'open-webui', logGroup }),
      portMappings: [{ containerPort: 8080, protocol: ecs.Protocol.TCP }],
      // The official image's start.sh ships unpatched, so DATABASE_URL is
      // composed here from the component vars + the Secrets-Manager-injected
      // password rather than baked into the image. The generated password is
      // URL-safe (see data-stack.ts excludeCharacters).
      command: ['/bin/sh', '-lc',
        'export DATABASE_URL="postgresql://${DATABASE_USER}:${DATABASE_PASSWORD}@${DATABASE_HOST}:${DATABASE_PORT}/${DATABASE_NAME}" && ' +
        pipeBootstrap +
        'exec bash start.sh'],
      environment: {
        WEBUI_URL: '',
        PORT: '8080',
        STORAGE_PROVIDER: 's3',
        S3_BUCKET_NAME: uploadBucket.bucketName,
        S3_REGION_NAME: cdk.Aws.REGION,
        DATABASE_HOST: auroraCluster.clusterEndpoint.hostname,
        DATABASE_PORT: '5432',
        DATABASE_NAME: 'openwebui',
        DATABASE_USER: 'postgres',
        REDIS_URL: `rediss://${redisEndpoint}:6379`,
        // WebSocket / Socket.IO. CloudFront supports WebSocket over VPC origins
        // (GA 2026-05-01), and Open WebUI defaults ENABLE_WEBSOCKET_SUPPORT to
        // 'True' when unset — so the live tasks have ALREADY been serving
        // websocket-only transport (polling is rejected). Pin it explicitly so
        // realtime can't silently break on an upstream default change or an
        // accidental flip: the SPA uses websocket-only transport when WS is on,
        // with no polling fallback (src/routes/+layout.svelte).
        ENABLE_WEBSOCKET_SUPPORT: 'true',
        // Share Socket.IO state across tasks via the Redis manager. Without this
        // (WEBSOCKET_MANAGER defaults to empty) each task keeps session/room
        // state in-process, so any scale-out beyond a single task breaks realtime
        // (missed events, broken event_call round-trips) unless ALB stickiness is
        // also enabled. Prod runs autoscaling up to 10 tasks, so the manager is
        // required, not optional. Redis (ElastiCache) is already provisioned.
        WEBSOCKET_MANAGER: 'redis',
        WEBSOCKET_REDIS_URL: `rediss://${redisEndpoint}:6379/0`,
        // OIDC/OAuth — Cognito as standard OIDC provider
        ENABLE_OAUTH_SIGNUP: 'true',
        OAUTH_CLIENT_ID: userPoolClient.userPoolClientId,
        OPENID_PROVIDER_URL: `https://cognito-idp.${cdk.Aws.REGION}.amazonaws.com/${userPool.userPoolId}/.well-known/openid-configuration`,
        OAUTH_PROVIDER_NAME: 'Amazon Cognito',
        OAUTH_SCOPES: 'openid email profile',
        OPENID_REDIRECT_URI: cdk.Lazy.string({
          produce: () => props.domainName
            ? `https://${props.domainName}/oauth/oidc/callback`
            : `https://${this.distribution.distributionDomainName}/oauth/oidc/callback`,
        }),
        ENABLE_OAUTH_PERSISTENT_CONFIG: 'false',
        OAUTH_USERNAME_CLAIM: 'email',
        ENABLE_OAUTH_ROLE_MANAGEMENT: 'true',
        OAUTH_ROLES_CLAIM: 'cognito:groups',
        OAUTH_ADMIN_ROLES: 'admin,webui-admins,admins',
        OAUTH_ALLOWED_ROLES: 'admin,webui-admins,admins,user,power-users,basic-users',
        ENABLE_OAUTH_GROUP_MANAGEMENT: 'true',
        OAUTH_GROUP_CLAIM: 'cognito:groups',
        ENABLE_OAUTH_GROUP_CREATION: 'true',
        OAUTH_MERGE_ACCOUNTS_BY_EMAIL: 'true',
        WEBUI_AUTH_SIGNOUT_REDIRECT_URL: cdk.Lazy.string({
          produce: () => {
            const appHost = props.domainName ?? this.distribution.distributionDomainName;
            return `https://${userPoolDomainName}/logout?client_id=${userPoolClient.userPoolClientId}&logout_uri=https://${appHost}/auth`;
          },
        }),
        OPENID_END_SESSION_ENDPOINT: cdk.Lazy.string({
          produce: () => {
            const appHost = props.domainName ?? this.distribution.distributionDomainName;
            return `https://${userPoolDomainName}/logout?client_id=${userPoolClient.userPoolClientId}&logout_uri=https://${appHost}/auth`;
          },
        }),
        ENABLE_OLLAMA_API: 'false',
        // The Bedrock inference gateway (…/inference) and the region whose
        // bedrock-mantle catalog it fronts. Read by the seeder to configure the
        // OpenAI connections + the Claude pipe. Not consumed by upstream itself.
        GATEWAY_INFERENCE_URL: props.gatewayInferenceUrl,
        MANTLE_REGION: props.mantleRegion ?? cdk.Aws.REGION,
        // Opt-in metering module (docs/METERING.md): read by metering_seed.py
        // and the seeded filter. Spread ⇒ ZERO env delta when metering is off.
        ...(props.metering
          ? {
              METERING_ENABLED: 'true',
              METERING_BUS: props.metering.busName,
              METERING_TABLE: props.metering.tableName,
              METERING_REGION: cdk.Aws.REGION,
            }
          : {}),
        // Single-SSO deployment — skip Open WebUI's local login form and go
        // straight to the Cognito hosted UI. Auto-redirect only fires when the
        // local login form is disabled (auth/+page.svelte gates on
        // enable_login_form === false), so the two flags travel together.
        // Local-auth escape hatch remains /auth?form=1 (renders the form even
        // with ENABLE_LOGIN_FORM=false and suppresses the redirect).
        OAUTH_AUTO_REDIRECT: 'true',
        ENABLE_LOGIN_FORM: 'false',
        // Encrypt tool/function valve values at rest (Fernet keyed off
        // WEBUI_SECRET_KEY). Existing plaintext valve rows stay readable
        // (decrypt_valves passes dicts through) and encrypt on next save.
        ENABLE_VALVE_ENCRYPTION: 'true',
        // Store vectors in Aurora pgvector instead of the default on-container
        // Chroma (DATA_DIR has no durable volume, so it is wiped on every task
        // restart). PGVECTOR_DB_URL defaults to DATABASE_URL;
        // PGVECTOR_CREATE_EXTENSION defaults true and the app connects as the
        // master user, so first boot creates the extension.
        VECTOR_DB: 'pgvector',
      },
      secrets: {
        WEBUI_SECRET_KEY: ecs.Secret.fromSecretsManager(webuiSecret, 'WEBUI_SECRET_KEY'),
        OAUTH_CLIENT_SECRET: ecs.Secret.fromSecretsManager(cognitoClientSecret),
        DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(auroraCluster.secret!, 'password'),
      },
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8080/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // Keep the execution role's READ on the Aurora secret explicit: rollback
    // task-definition revisions that still declare the secret fail at
    // provisioning if the role loses the grant.
    auroraCluster.secret!.grantRead(taskDefinition.executionRole!);

    // =====================
    // Internal ALB (private subnets — not internet-facing)
    // CloudFront connects via VPC origin
    // =====================
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'ALB', {
      vpc,
      internetFacing: false,
      securityGroup: albSecurityGroup,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      // Raise the idle timeout well above Socket.IO's ping interval (~25s) and
      // typical long-generation gaps so neither the WebSocket nor any SSE/long
      // request is reset mid-stream. The default 60s is tight for idle sockets
      // and long model generations.
      idleTimeout: cdk.Duration.seconds(3600),
    });

    // =====================
    // ECS Service
    // =====================
    this.fargateService = new ecs.FargateService(this, 'FargateService', {
      cluster: this.ecsCluster,
      taskDefinition,
      desiredCount: desiredCount,
      securityGroups: [ecsSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      // 180s grace: first task on a new image digest needs time to pull the
      // ~2-3 GB container (cold ECR cache) and warm up the ML models loaded
      // during container startup before /health can respond reliably.
      healthCheckGracePeriod: cdk.Duration.seconds(180),
      // Automatic rollback to the previous task definition if the new
      // deployment fails to stabilize. Prevents the service from getting
      // stuck after a bad image or bad task definition revision.
      circuitBreaker: { rollback: true },
      // 2026-07-01 incident hardening: a deployment isn't "done" the moment
      // one task passes ELB checks — hold it for a bake window so a task that
      // boots, registers, then crash-loops (the IAM fe_sendauth mode) flips
      // the deployment to FAILED+rollback instead of COMPLETED followed by
      // 20+ minutes of churn.
      bakeTime: cdk.Duration.minutes(5),
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'TargetGroup', {
      vpc,
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: { path: '/health', healthyHttpCodes: '200', interval: cdk.Duration.seconds(30), timeout: cdk.Duration.seconds(5) },
    });

    this.fargateService.attachToApplicationTargetGroup(targetGroup);

    this.alb.addListener('HTTPListener', {
      port: 80,
      defaultAction: elbv2.ListenerAction.forward([targetGroup]),
    });

    // 2026-07-01 incident hardening: alarm-gated deployments. HealthyHostCount
    // < 1 for 2 consecutive minutes during a deployment (+ bake) triggers
    // rollback — and the same alarm is the operator's page for the
    // desired-count-0 / all-tasks-exited outage modes outside deployments.
    // (Must come after the listener attaches the target group to the ALB, or
    // metricHealthyHostCount() throws TargetGroupNeedsAttachedLoad at synth.)
    const healthyHostAlarm = targetGroup
      .metricHealthyHostCount({ period: cdk.Duration.minutes(1), statistic: 'Minimum' })
      .createAlarm(this, 'HealthyHostAlarm', {
        alarmName: `${props.environmentPrefix ?? 'owui'}-open-webui-no-healthy-hosts`,
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 2,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      });
    this.fargateService.enableDeploymentAlarms([healthyHostAlarm.alarmName], {
      behavior: ecs.AlarmBehavior.ROLLBACK_ON_ALARM,
    });

    // =====================
    // CloudFront with VPC Origin (connects to internal ALB)
    // =====================
    const vpcOriginResource = new cloudfront.VpcOrigin(this, 'VpcOrigin', {
      endpoint: cloudfront.VpcOriginEndpoint.applicationLoadBalancer(this.alb),
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
    });

    const vpcOrigin = origins.VpcOrigin.withVpcOrigin(vpcOriginResource);

    const distributionProps: cloudfront.DistributionProps = {
      defaultBehavior: {
        origin: vpcOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
      },
    };

    if (props.domainName && props.certificateArn) {
      const certificate = acm.Certificate.fromCertificateArn(this, 'Certificate', props.certificateArn);
      (distributionProps as any).domainNames = [props.domainName];
      (distributionProps as any).certificate = certificate;
    }

    this.distribution = new cloudfront.Distribution(this, 'Distribution', distributionProps);

    // =====================
    // Auto Scaling
    // =====================
    if (enableAutoScaling) {
      const scaling = this.fargateService.autoScaleTaskCount({ minCapacity: minCapacity, maxCapacity: maxCapacity });
      scaling.scaleOnCpuUtilization('CpuScaling', { targetUtilizationPercent: 70, scaleInCooldown: cdk.Duration.seconds(300), scaleOutCooldown: cdk.Duration.seconds(60) });
      scaling.scaleOnMemoryUtilization('MemoryScaling', { targetUtilizationPercent: 80, scaleInCooldown: cdk.Duration.seconds(300), scaleOutCooldown: cdk.Duration.seconds(60) });
    }

    // =====================
    // Outputs
    // =====================
    new cdk.CfnOutput(this, 'DistributionDomainName', { value: this.distribution.distributionDomainName });
    new cdk.CfnOutput(this, 'DistributionId', { value: this.distribution.distributionId });
    new cdk.CfnOutput(this, 'AppUrl', {
      value: props.domainName ? `https://${props.domainName}` : `https://${this.distribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, 'AppImageUri', {
      value: imageUriForOutput,
      description: 'Container image URI deployed to ECS',
    });
    new cdk.CfnOutput(this, 'ServiceName', { value: this.fargateService.serviceName });
  }
}
