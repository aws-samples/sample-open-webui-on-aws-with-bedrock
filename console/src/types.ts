// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// API response shapes — mirrors metering/admin-api/index.py.

export interface ConsoleConfig {
  region: string;
  userPoolId: string;
  clientId: string;
  cognitoDomain: string;
  apiBase: string;
}

export interface ModuleConfig {
  enforce_mode: string;
  price_map_version: string;
  admin_groups: string[];
  defaults: { hard_limit_usd: number; soft_limit_usd: number; rpm_limit: number };
  window: string;
  is_admin: boolean;
}

export interface UserRow {
  sub: string;
  email?: string;
  status?: string;
  enabled?: boolean;
  used_usd?: number;
  est_usd?: number;
  total_usd: number;
  used_in?: number;
  used_out?: number;
  req_count?: number;
  hard_limit_usd: number;
  soft_limit_usd: number;
  pct_of_limit: number;
  updated_at?: number;
  alerted?: string;
}

export interface GroupRow {
  group: string;
  total_usd: number;
  used_usd?: number;
  used_in?: number;
  used_out?: number;
  req_count?: number;
  pct_of_limit: number;
  ceiling_usd?: number;
  pct_of_ceiling?: number | null;
  updated_at?: number;
}

export interface Policy {
  scope: string;
  hard_limit_usd: number;
  soft_limit_usd?: number;
  rpm_limit?: number;
  note?: string;
  override_until?: number;
  updated_by?: string;
  updated_at?: number;
}

export interface PolicyChain {
  effective: Partial<Policy>;
  source: string;
  chain: {
    user_override: Partial<Policy> | null;
    default: Partial<Policy> | null;
    environment: Partial<Policy>;
  };
}

export interface LedgerCall {
  ts: number;
  sub?: string;
  email?: string;
  model?: string;
  lane?: string;
  tier?: string;
  tokens_in?: number;
  tokens_out?: number;
  tokens_cached?: number;
  usd?: number;
  state?: string;
  source?: string;
  billing_group?: string;
  price_map_version?: string;
}

export interface OpenEstimate {
  estimate_key: string;
  sub?: string;
  window?: string;
  model?: string;
  lane?: string;
  usd?: number;
  created_at?: number;
  billing_group?: string;
}

export interface UserDetail {
  sub: string;
  window: string;
  profile: { email?: string; name?: string; status?: string; enabled?: boolean; created?: string };
  groups: string[];
  is_admin_group_member: boolean;
  counter: Record<string, number | string>;
  total_usd: number;
  pct_of_limit: number;
  policy: PolicyChain;
  open_estimates: OpenEstimate[];
  recent_calls: LedgerCall[];
}

export interface AuditEntry {
  ts: number;
  actor: string;
  action: string;
  target: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
}

export interface Alarm {
  name: string;
  state: 'OK' | 'ALARM' | 'INSUFFICIENT_DATA';
  reason: string;
  since: string;
  metric: string;
}

export interface MetricsResponse {
  range: string;
  period_seconds: number;
  series: Record<string, [number, number][]>;
}

export interface AlertSubscription {
  arn: string;
  protocol: string;
  endpoint: string;
  pending: boolean;
}

export interface SearchUser {
  sub: string;
  email: string;
  name?: string;
  status: string;
  enabled: boolean;
  groups: string[];
}

export interface UsageMe {
  sub: string;
  window: string;
  counter: Record<string, number | string>;
  policy: Partial<Policy>;
  resolved?: PolicyChain;
}
