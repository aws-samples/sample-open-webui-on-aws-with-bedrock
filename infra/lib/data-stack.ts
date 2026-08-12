// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as elasticache from 'aws-cdk-lib/aws-elasticache';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface DataStackProps extends cdk.StackProps {
  vpc: ec2.Vpc;
  ecsSecurityGroup: ec2.SecurityGroup;
  auroraMinCapacity?: number;
  auroraMaxCapacity?: number;
  auroraDeletionProtection?: boolean;
}

export class DataStack extends cdk.Stack {
  public readonly auroraCluster: rds.DatabaseCluster;
  public readonly redisReplicationGroup: elasticache.CfnReplicationGroup;
  public readonly uploadBucket: s3.Bucket;
  public readonly dbSecurityGroup: ec2.SecurityGroup;
  public readonly redisSecurityGroup: ec2.SecurityGroup;
  public readonly redisEndpoint: string;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    const { vpc, ecsSecurityGroup } = props;

    // =====================
    // Aurora PostgreSQL Serverless v2
    // =====================

    this.dbSecurityGroup = new ec2.SecurityGroup(this, 'AuroraSecurityGroup', {
      vpc,
      description: 'Security group for Aurora PostgreSQL',
      allowAllOutbound: false,
    });

    // Allow ECS tasks to access Aurora
    this.dbSecurityGroup.addIngressRule(
      ecsSecurityGroup,
      ec2.Port.tcp(5432),
      'Allow ECS tasks to access Aurora PostgreSQL'
    );

    this.auroraCluster = new rds.DatabaseCluster(this, 'AuroraCluster', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_4,
      }),
      vpc,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
      },
      securityGroups: [this.dbSecurityGroup],
      serverlessV2MinCapacity: props.auroraMinCapacity ?? 0.5,
      serverlessV2MaxCapacity: props.auroraMaxCapacity ?? 8,
      writer: rds.ClusterInstance.serverlessV2('Writer', {
        publiclyAccessible: false,
      }),
      readers: [
        rds.ClusterInstance.serverlessV2('Reader', {
          scaleWithWriter: true,
          publiclyAccessible: false,
        }),
      ],
      defaultDatabaseName: 'openwebui',
      storageEncrypted: true,
      deletionProtection: props.auroraDeletionProtection ?? true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      // The generated admin password must be URL-safe: the compute stack
      // composes DATABASE_URL from these credentials as a plain connection
      // string, so exclude every character that would need URL-encoding.
      credentials: rds.Credentials.fromGeneratedSecret('postgres', {
        excludeCharacters: ' /:@?#"\\\'`%&=+<>[]{}|^~',
      }),
    });

    // =====================
    // ElastiCache Redis
    // =====================

    this.redisSecurityGroup = new ec2.SecurityGroup(this, 'RedisSecurityGroup', {
      vpc,
      description: 'Security group for ElastiCache Redis',
      allowAllOutbound: false,
    });

    // Allow ECS tasks to access Redis
    this.redisSecurityGroup.addIngressRule(
      ecsSecurityGroup,
      ec2.Port.tcp(6379),
      'Allow ECS tasks to access ElastiCache Redis'
    );

    const redisSubnetGroup = new elasticache.CfnSubnetGroup(
      this,
      'RedisSubnetGroup',
      {
        description: 'Subnet group for ElastiCache Redis',
        subnetIds: vpc.selectSubnets({
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        }).subnetIds,
      }
    );

    this.redisReplicationGroup = new elasticache.CfnReplicationGroup(this, 'RedisReplicationGroup', {
      replicationGroupDescription: 'Open WebUI Redis',
      engine: 'redis',
      cacheNodeType: 'cache.t3.micro',
      numCacheClusters: 1,
      cacheSubnetGroupName: redisSubnetGroup.ref,
      securityGroupIds: [this.redisSecurityGroup.securityGroupId],
      transitEncryptionEnabled: true,
      atRestEncryptionEnabled: true,
      automaticFailoverEnabled: false,
    });

    this.redisEndpoint = this.redisReplicationGroup.attrPrimaryEndPointAddress;

    // =====================
    // S3 Bucket for file uploads
    // =====================

    this.uploadBucket = new s3.Bucket(this, 'UploadBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      versioned: true,
      lifecycleRules: [
        {
          id: 'CleanupOldVersions',
          noncurrentVersionExpiration: cdk.Duration.days(30),
        },
        {
          id: 'TransitionToIA',
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(90),
            },
          ],
        },
      ],
    });

    // =====================
    // Outputs
    // =====================

    new cdk.CfnOutput(this, 'AuroraClusterEndpoint', {
      value: this.auroraCluster.clusterEndpoint.hostname,
      description: 'Aurora PostgreSQL cluster endpoint',
    });

    new cdk.CfnOutput(this, 'RedisEndpoint', {
      value: this.redisEndpoint,
      description: 'ElastiCache Redis endpoint',
    });

    new cdk.CfnOutput(this, 'S3BucketName', {
      value: this.uploadBucket.bucketName,
      description: 'S3 bucket for file uploads',
    });
  }
}
