// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

export function usd(v: number | undefined | null, digits = 2): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '–';
  if (v !== 0 && Math.abs(v) < 0.01 && digits <= 2) return `$${v.toFixed(4)}`;
  return v.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: Math.max(digits, 2),
  });
}

export function tokens(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '–';
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 10_000) return `${(v / 1_000).toFixed(1)}K`;
  return v.toLocaleString('en-US');
}

export function epochToLocal(ts: number | undefined): string {
  if (!ts) return '–';
  return new Date(ts * 1000).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function ago(ts: number | undefined): string {
  if (!ts) return '–';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** Current + recent YYYY-MM windows for the window picker. */
export function recentWindows(n = 6): string[] {
  const out: string[] = [];
  const d = new Date();
  for (let i = 0; i < n; i++) {
    out.push(`${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`);
    d.setUTCMonth(d.getUTCMonth() - 1);
  }
  return out;
}

export function shortSub(sub: string | undefined): string {
  if (!sub) return '–';
  return sub.length > 13 ? `${sub.slice(0, 8)}…${sub.slice(-4)}` : sub;
}
