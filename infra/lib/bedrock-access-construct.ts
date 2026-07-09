// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface BedrockAccessConstructProps {
  /**
   * List of specific Bedrock model IDs or ARN patterns to allow.
   * If empty, grants access to all foundation models and inference profiles.
   */
  allowedModels?: string[];
}

export class BedrockAccessConstruct extends Construct {
  public readonly policyStatements: iam.PolicyStatement[];

  constructor(scope: Construct, id: string, props?: BedrockAccessConstructProps) {
    super(scope, id);

    const allowedModels = props?.allowedModels || [];
    this.policyStatements = [];

    // List models and inference profiles
    this.policyStatements.push(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:ListFoundationModels',
          'bedrock:ListInferenceProfiles',
          'bedrock:GetInferenceProfile',
        ],
        resources: ['*'],
      })
    );

    // Model invocation via foundation models
    let modelResources: string[];
    if (allowedModels.length > 0) {
      modelResources = allowedModels.map(
        (modelId) => `arn:aws:bedrock:*::foundation-model/${modelId}`
      );
    } else {
      modelResources = ['arn:aws:bedrock:*::foundation-model/*'];
    }

    // Cross-region inference profile invocation
    const profileResources = ['arn:aws:bedrock:*:*:inference-profile/*'];

    this.policyStatements.push(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: [...modelResources, ...profileResources],
      })
    );
  }
}
