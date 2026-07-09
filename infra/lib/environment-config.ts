// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
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
