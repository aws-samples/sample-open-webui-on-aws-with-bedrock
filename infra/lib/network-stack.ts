// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

export interface NetworkStackProps extends cdk.StackProps {
  maxAzs?: number;
}

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly ecsSecurityGroup: ec2.SecurityGroup;
  public readonly albSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props?: NetworkStackProps) {
    super(scope, id, props);

    const maxAzs = props?.maxAzs ?? 2;

    // VPC with public and private subnets
    this.vpc = new ec2.Vpc(this, 'OpenWebUIVpc', {
      maxAzs,
      natGateways: maxAzs,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
    });

    // VPC Endpoints for AWS services
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    this.vpc.addInterfaceEndpoint('EcrEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR,
    });

    this.vpc.addInterfaceEndpoint('EcrDockerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    });

    this.vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    });

    this.vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
    });

    this.vpc.addInterfaceEndpoint('BedrockEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
    });

    // Security group for ECS tasks (shared reference for other stacks)
    this.ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsTaskSG', {
      vpc: this.vpc,
      description: 'Security group for ECS Fargate tasks',
      allowAllOutbound: true,
    });

    // Security group for ALB (internal — reached only by CloudFront's VPC origin)
    this.albSecurityGroup = new ec2.SecurityGroup(this, 'ALBSecurityGroup', {
      vpc: this.vpc,
      description: 'Security group for internal ALB',
      allowAllOutbound: true,
    });

    // Allow CloudFront's VPC origin to reach the ALB on port 80.
    //
    // CRITICAL: CloudFront does NOT add this rule for you. When CloudFront
    // creates a VPC origin it provisions service-managed ENIs in the VPC behind
    // its own security group, but it never edits the *target* (ALB) security
    // group. Without an explicit inbound rule the ALB SG default-denies and every
    // request — HTTP page load and Socket.IO WebSocket upgrade alike — fails with
    // HTTP 504. (The earlier `// CloudFront ... manages security group rules`
    // comment was wrong; the live stacks only work because an older revision left
    // an `0.0.0.0/0:80` rule in the deployed template. Re-synthesizing without a
    // rule here would DELETE that orphan and 504 the site.)
    //
    // We admit the CloudFront origin-facing managed prefix list (AWS's documented
    // option for VPC origins) rather than the whole internet. The ALB is internal,
    // so it is not otherwise reachable; the ALB→target health check originates
    // inside the VPC and is unaffected. The prefix-list id is region-specific:
    // by default we resolve it via a context lookup (real deploys have creds); for
    // offline synth / credential-free CI pass `-c cloudfrontPrefixListId=pl-xxxx`.
    const overridePrefixListId = this.node.tryGetContext('cloudfrontPrefixListId') as string | undefined;
    const cloudFrontPeer = overridePrefixListId
      ? ec2.Peer.prefixList(overridePrefixListId)
      : ec2.Peer.prefixList(
          ec2.PrefixList.fromLookup(this, 'CloudFrontOriginFacing', {
            prefixListName: 'com.amazonaws.global.cloudfront.origin-facing',
          }).prefixListId,
        );
    this.albSecurityGroup.addIngressRule(
      cloudFrontPeer,
      ec2.Port.tcp(80),
      'CloudFront VPC origin to internal ALB',
    );

    // Allow ALB to reach ECS tasks
    this.ecsSecurityGroup.addIngressRule(
      this.albSecurityGroup,
      ec2.Port.tcp(8080),
      'Allow ALB to reach ECS tasks'
    );

    // Outputs
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: 'VPC ID',
    });
  }
}
