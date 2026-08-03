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
  pricing?: {
    catalog_version?: Record<string, string> | string | null;
    refresh_generation?: number;
    model_count?: number;
    unmatched_count?: number;
    refreshed_at?: number | null;
    partial?: boolean;
    region?: string;
  };
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
  usd_estimate?: number;
  unpriced?: boolean;
  state?: string;
  source?: string;
  billing_group?: string;
  price_map_version?: string;
  price_source?: string;
  routing?: string;
  rate_fallback?: boolean;
  matched_routing?: string;
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

/** routing → tier → context → direction → USD per 1M tokens */
export type RateGrid = Record<string, Record<string, Record<string, Record<string, number>>>>;

export interface PriceOverride {
  /** current contract: per-1M rates map */
  rates?: Record<string, number>;
  scope?: string;
  _UNIT?: string;
  /** legacy (pre-migration) flat per-token attrs, still honored by the resolver */
  input?: number;
  output?: number;
  note?: string;
  updated_by?: string;
  updated_at?: number;
}

export interface PriceRow {
  model: string;
  display_name: string;
  provider: string;
  /** every catalog key this model resolves under (canonical + alias keys) */
  keys: string[];
  routing_modes: string[];
  rates?: RateGrid | null;
  resolved_via?: 'direct-id' | 'control-plane-name' | 'alias' | string;
  price_list_name?: string | null;
  offer_version?: string | null;
  /** effective standard in-region rate, USD per 1M tokens */
  effective: {
    input_per_1m: number | null;
    output_per_1m: number | null;
    source: 'override' | 'aws-published' | 'unpriced';
  };
  override?: PriceOverride | null;
}

export interface UnmatchedRow {
  price_list_name: string;
  provider?: string;
  service_code?: string;
  reason: 'no-control-plane-match' | 'ambiguous-match' | 'no-token-rates' | string;
  candidate_rates?: RateGrid;
  refresh_generation?: number;
  updated_at?: number;
}

export interface PricingAlias {
  price_list_name: string;
  model_id?: string;
  updated_by?: string;
  updated_at?: number;
}

export interface PricingCatalog {
  models: PriceRow[];
  unmatched: UnmatchedRow[];
  aliases: PricingAlias[];
  count: number;
  _UNIT?: string;
  meta: {
    offer_versions?: Record<string, string>;
    refresh_generation?: number;
    model_count?: number;
    row_count?: number;
    unmatched_count?: number;
    alias_count?: number;
    region?: string;
    refreshed_at?: number;
    partial?: boolean;
  };
}

export interface UsageMe {
  sub: string;
  window: string;
  counter: Record<string, number | string>;
  policy: Partial<Policy>;
  resolved?: PolicyChain;
}
