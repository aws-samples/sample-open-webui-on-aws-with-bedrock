// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Deploy-time config: fetched from /config.json (written by the CDK
// BucketDeployment) so no pool/client ids are ever baked into the bundle.

import { createContext, useContext } from 'react';
import type { ConsoleConfig } from './types';

export const ConfigContext = createContext<ConsoleConfig | null>(null);

export function useConfig(): ConsoleConfig {
  const cfg = useContext(ConfigContext);
  if (!cfg) throw new Error('config not loaded');
  return cfg;
}

export async function loadConfig(): Promise<ConsoleConfig> {
  const res = await fetch('/config.json', { cache: 'no-store' });
  if (!res.ok) throw new Error(`config.json: HTTP ${res.status}`);
  return (await res.json()) as ConsoleConfig;
}

/** Cognito Managed Login sign-out (clears the hosted session, then returns). */
export function cognitoSignOut(cfg: ConsoleConfig): void {
  const url =
    `https://${cfg.cognitoDomain}/logout?client_id=${encodeURIComponent(cfg.clientId)}` +
    `&logout_uri=${encodeURIComponent(`${window.location.origin}/`)}`;
  window.location.assign(url);
}
