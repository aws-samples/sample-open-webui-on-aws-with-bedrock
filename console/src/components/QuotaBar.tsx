// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Quota consumption bar: status color is a STATE encoding (ok / warning ≥80% /
// critical ≥100%), always paired with the % label — never color alone.

import { Box, ProgressBar, StatusIndicator } from '@cloudscape-design/components';
import { usd } from '../format';

export function QuotaBar(props: { totalUsd: number; hardUsd: number; pct: number }) {
  const { totalUsd, hardUsd, pct } = props;
  if (!hardUsd || hardUsd <= 0) {
    return <Box color="text-status-inactive">no limit</Box>;
  }
  return (
    <ProgressBar
      value={Math.min(pct, 100)}
      status={pct >= 100 ? 'error' : undefined}
      description={`${usd(totalUsd)} of ${usd(hardUsd)}`}
      resultText=""
    />
  );
}

export function QuotaStatus(props: { pct: number }) {
  const { pct } = props;
  if (pct >= 100) return <StatusIndicator type="error">Over limit</StatusIndicator>;
  if (pct >= 80) return <StatusIndicator type="warning">{pct.toFixed(0)}% used</StatusIndicator>;
  return <StatusIndicator type="success">{pct.toFixed(0)}% used</StatusIndicator>;
}
