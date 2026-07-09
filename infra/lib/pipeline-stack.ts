// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as codepipeline from 'aws-cdk-lib/aws-codepipeline';
import * as codepipeline_actions from 'aws-cdk-lib/aws-codepipeline-actions';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface PipelineStackProps extends cdk.StackProps {
  /** CodeStar Connection ARN for GitHub */
  connectionArn: string;
  /** GitHub repo owner */
  repoOwner: string;
  /** GitHub repo name */
  repoName: string;
  /** Branch to trigger pipeline */
  branch?: string;
  /** Email for approval notifications */
  approvalEmail?: string;
  /** Dev CloudFront URL for smoke tests (without https://) */
  devUrl?: string;
  /** Dev custom domain name (e.g., "dev-oui.example.com") */
  devDomainName?: string;
  /** ARN of ACM certificate for the dev custom domain */
  devCertificateArn?: string;
  /** Prod custom domain name (e.g., "oui.example.com") */
  prodDomainName?: string;
  /** ARN of ACM certificate for the prod custom domain */
  prodCertificateArn?: string;
}

/**
 * CI/CD pipeline for Open WebUI.
 *
 * Pipeline shape:
 *   Source → Deploy-Dev → Test+Approve → Deploy-Prod
 *
 * Both deploy stages invoke `cdk deploy`, which uses a DockerImageAsset
 * (see ComputeStack) to build and push the container image during deploy.
 * The asset hash is deterministic from the source tree, so:
 *
 *   - Deploy-Dev builds + pushes the image into the CDK asset ECR.
 *   - Deploy-Prod computes the same asset hash, finds the image already
 *     in ECR, and skips the build (bit-identical promotion).
 *
 * This replaces the previous Build stage (buildspec.yml + named
 * open-webui ECR repo + IMAGE_TAG plumbing).
 */
export class PipelineStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PipelineStackProps) {
    super(scope, id, props);

    const branch = props.branch ?? 'main';

    // Artifact bucket
    const artifactBucket = new s3.Bucket(this, 'ArtifactBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
    });

    // Source
    const sourceOutput = new codepipeline.Artifact('SourceOutput');
    const sourceAction = new codepipeline_actions.CodeStarConnectionsSourceAction({
      actionName: 'GitHub',
      owner: props.repoOwner,
      repo: props.repoName,
      branch,
      output: sourceOutput,
      connectionArn: props.connectionArn,
    });

    // CDK deploy role — shared by dev and prod deploy actions.
    // The cdk-* roles (assumed via sts:AssumeRole) are the CDK bootstrap
    // toolkit's file-publishing, image-publishing, lookup, deploy, and
    // exec roles. DockerImageAsset uses the image-publishing-role to push
    // into the bootstrap-managed asset ECR.
    const cdkDeployRole = new iam.Role(this, 'CdkDeployRole', {
      assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
      inlinePolicies: {
        CdkDeploy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['sts:AssumeRole'],
              resources: [`arn:aws:iam::${cdk.Aws.ACCOUNT_ID}:role/cdk-*`],
            }),
            // Post-deploy: sync Cognito client secret to Secrets Manager
            new iam.PolicyStatement({
              actions: ['cloudformation:DescribeStacks'],
              resources: [`arn:aws:cloudformation:*:${cdk.Aws.ACCOUNT_ID}:stack/OpenWebUI-*`],
            }),
            new iam.PolicyStatement({
              actions: ['cognito-idp:DescribeUserPoolClient'],
              resources: [`arn:aws:cognito-idp:*:${cdk.Aws.ACCOUNT_ID}:userpool/*`],
            }),
            new iam.PolicyStatement({
              actions: ['secretsmanager:PutSecretValue'],
              resources: [`arn:aws:secretsmanager:*:${cdk.Aws.ACCOUNT_ID}:secret:open-webui/*`],
            }),
          ],
        }),
      },
    });

    // Shared CodeBuild environment for deploy projects.
    // - privileged: true          — needed for docker build inside cdk deploy
    // - computeType: LARGE        — image build downloads ~2-3 GB of ML models
    // - buildImage: STANDARD_7_0  — provides Docker + buildx for linux/amd64
    const deployBuildEnvironment: codebuild.BuildEnvironment = {
      buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
      privileged: true,
      computeType: codebuild.ComputeType.LARGE,
    };

    // Local Docker layer cache — speeds up repeat deploys when source
    // tree hasn't meaningfully changed.
    const deployCache = codebuild.Cache.local(
      codebuild.LocalCacheMode.DOCKER_LAYER,
      codebuild.LocalCacheMode.CUSTOM,
    );

    // Dev deploy project
    const devDeployProject = new codebuild.PipelineProject(this, 'DevDeployProject', {
      projectName: 'OpenWebUI-Deploy-Dev',
      role: cdkDeployRole,
      environment: deployBuildEnvironment,
      cache: deployCache,
      environmentVariables: {
        DEV_URL: { value: props.devUrl ?? '' },
        DEV_DOMAIN_NAME: { value: props.devDomainName ?? '' },
        DEV_CERTIFICATE_ARN: { value: props.devCertificateArn ?? '' },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          install: {
            'runtime-versions': { nodejs: 22 },
            commands: ['npm install -g aws-cdk'],
          },
          build: {
            commands: [
              'cd infra && npm ci',
              // CDK builds + pushes the container image via DockerImageAsset
              // during this deploy. GIT_COMMIT is stamped into WEBUI_BUILD_VERSION.
              'export GIT_COMMIT=$CODEBUILD_RESOLVED_SOURCE_VERSION',
              'CDK_CONTEXT="-c environment=dev -c devUrl=$DEV_URL"',
              'if [ -n "$DEV_DOMAIN_NAME" ]; then CDK_CONTEXT="$CDK_CONTEXT -c devDomainName=$DEV_DOMAIN_NAME"; fi',
              'if [ -n "$DEV_CERTIFICATE_ARN" ]; then CDK_CONTEXT="$CDK_CONTEXT -c devCertificateArn=$DEV_CERTIFICATE_ARN"; fi',
              'eval "npx cdk deploy --all $CDK_CONTEXT --require-approval never"',
            ],
          },
          post_build: {
            commands: [
              // Sync Cognito client secret to Secrets Manager
              'POOL_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Auth --query "Stacks[0].Outputs[?OutputKey==\'UserPoolId\'].OutputValue" --output text)',
              'CLIENT_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Dev-Auth --query "Stacks[0].Outputs[?OutputKey==\'UserPoolClientId\'].OutputValue" --output text)',
              'CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client --user-pool-id $POOL_ID --client-id $CLIENT_ID --query "UserPoolClient.ClientSecret" --output text)',
              'aws secretsmanager put-secret-value --secret-id open-webui/dev-cognito-client-secret --secret-string "$CLIENT_SECRET"',
              'echo "Cognito client secret synced to Secrets Manager"',
            ],
          },
        },
      }),
    });

    const devDeployAction = new codepipeline_actions.CodeBuildAction({
      actionName: 'Deploy-Dev',
      project: devDeployProject,
      input: sourceOutput,
    });

    // Smoke test project
    const smokeTestProject = new codebuild.PipelineProject(this, 'SmokeTestProject', {
      projectName: 'OpenWebUI-SmokeTest',
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.SMALL,
      },
      environmentVariables: {
        DEV_URL: { value: props.devUrl ?? '' },
      },
      buildSpec: codebuild.BuildSpec.fromSourceFilename('buildspec-smoke.yml'),
    });

    const smokeTestAction = new codepipeline_actions.CodeBuildAction({
      actionName: 'SmokeTest',
      project: smokeTestProject,
      input: sourceOutput,
      type: codepipeline_actions.CodeBuildActionType.TEST,
      runOrder: 1,
    });

    // Manual approval
    const approvalTopic = new sns.Topic(this, 'ApprovalTopic', {
      topicName: 'OpenWebUI-Pipeline-Approval',
    });

    const approvalAction = new codepipeline_actions.ManualApprovalAction({
      actionName: 'Approve-Prod',
      notificationTopic: approvalTopic,
      notifyEmails: props.approvalEmail ? [props.approvalEmail] : undefined,
      additionalInformation: 'Review dev environment and approve production deployment.',
      runOrder: 2,
    });

    // Prod deploy project
    const prodDeployProject = new codebuild.PipelineProject(this, 'ProdDeployProject', {
      projectName: 'OpenWebUI-Deploy-Prod',
      role: cdkDeployRole,
      environment: deployBuildEnvironment,
      cache: deployCache,
      environmentVariables: {
        PROD_DOMAIN_NAME: { value: props.prodDomainName ?? '' },
        PROD_CERTIFICATE_ARN: { value: props.prodCertificateArn ?? '' },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          install: {
            'runtime-versions': { nodejs: 22 },
            commands: ['npm install -g aws-cdk'],
          },
          build: {
            commands: [
              'cd infra && npm ci',
              // Same source tree as Deploy-Dev ⇒ same DockerImageAsset hash ⇒
              // CDK detects the image already in the asset ECR and skips the
              // rebuild. Prod deploys the exact digest dev tested.
              'export GIT_COMMIT=$CODEBUILD_RESOLVED_SOURCE_VERSION',
              'CDK_CONTEXT="-c environment=prod"',
              'if [ -n "$PROD_DOMAIN_NAME" ]; then CDK_CONTEXT="$CDK_CONTEXT -c prodDomainName=$PROD_DOMAIN_NAME"; fi',
              'if [ -n "$PROD_CERTIFICATE_ARN" ]; then CDK_CONTEXT="$CDK_CONTEXT -c prodCertificateArn=$PROD_CERTIFICATE_ARN"; fi',
              'eval "npx cdk deploy --all $CDK_CONTEXT --require-approval never"',
            ],
          },
          post_build: {
            commands: [
              'POOL_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Prod-Auth --query "Stacks[0].Outputs[?OutputKey==\'UserPoolId\'].OutputValue" --output text)',
              'CLIENT_ID=$(aws cloudformation describe-stacks --stack-name OpenWebUI-Prod-Auth --query "Stacks[0].Outputs[?OutputKey==\'UserPoolClientId\'].OutputValue" --output text)',
              'CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client --user-pool-id $POOL_ID --client-id $CLIENT_ID --query "UserPoolClient.ClientSecret" --output text)',
              'aws secretsmanager put-secret-value --secret-id open-webui/prod-cognito-client-secret --secret-string "$CLIENT_SECRET"',
              'echo "Cognito client secret synced to Secrets Manager"',
            ],
          },
        },
      }),
    });

    const prodDeployAction = new codepipeline_actions.CodeBuildAction({
      actionName: 'Deploy-Prod',
      project: prodDeployProject,
      input: sourceOutput,
    });

    // Pipeline
    new codepipeline.Pipeline(this, 'Pipeline', {
      pipelineName: 'OpenWebUI-Pipeline',
      pipelineType: codepipeline.PipelineType.V2,
      artifactBucket,
      stages: [
        { stageName: 'Source', actions: [sourceAction] },
        { stageName: 'Deploy-Dev', actions: [devDeployAction] },
        { stageName: 'Test-and-Approve', actions: [smokeTestAction, approvalAction] },
        { stageName: 'Deploy-Prod', actions: [prodDeployAction] },
      ],
    });
  }
}
