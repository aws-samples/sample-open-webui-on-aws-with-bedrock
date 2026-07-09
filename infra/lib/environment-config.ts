// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
export interface ImageConfig {
  /**
   * 'build' (default): CDK builds this repo's overlay Dockerfile at deploy time.
   * 'registry': pull a prebuilt overlay image from the operator's own registry
   * (must have been built from this repo's Dockerfile — the official upstream
   * image alone has no Bedrock provider).
   */
  source?: 'build' | 'registry';
  /** ECR repository name holding the prebuilt overlay image (registry mode). */
  registry?: string;
  /** Image tag for registry mode (default 'latest'). */
  tag?: string;
  /**
   * Dockerfile target for build mode: 'backend' (default — official UI, Bedrock
   * config via env vars / REST API) or 'full' (rebuilds the UI with the admin
   * Connections Bedrock panel).
   */
  target?: 'backend' | 'full';
}

export interface EnvironmentConfig {
  environment: string;
  stackPrefix: string;
  auroraMinCapacity: number;
  auroraMaxCapacity: number;
  auroraDeletionProtection: boolean;
  ecsDesiredCount: number;
  ecsMinCapacity: number;
  ecsMaxCapacity: number;
  enableAutoScaling: boolean;
  domainName?: string;
  certificateArn?: string;
  image?: ImageConfig;
}

export function getDevConfig(): EnvironmentConfig {
  return {
    environment: 'dev',
    stackPrefix: 'OpenWebUI-Dev',
    auroraMinCapacity: 0.5,
    auroraMaxCapacity: 4,
    auroraDeletionProtection: false,
    ecsDesiredCount: 1,
    ecsMinCapacity: 1,
    ecsMaxCapacity: 1,
    enableAutoScaling: false,
  };
}

export function getProdConfig(): EnvironmentConfig {
  return {
    environment: 'prod',
    stackPrefix: 'OpenWebUI-Prod',
    auroraMinCapacity: 0.5,
    auroraMaxCapacity: 8,
    auroraDeletionProtection: true,
    ecsDesiredCount: 1,
    ecsMinCapacity: 1,
    ecsMaxCapacity: 10,
    enableAutoScaling: true,
  };
}
