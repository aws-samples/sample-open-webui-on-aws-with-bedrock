// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { Badge, Box, StatusIndicator, Table } from '@cloudscape-design/components';
import { epochToLocal, tokens, usd } from '../format';
import type { LedgerCall } from '../types';

export function LedgerTable(props: {
  calls: LedgerCall[];
  loading?: boolean;
  empty?: string;
  showUser?: boolean;
  userCell?: (c: LedgerCall) => React.ReactNode;
}) {
  const cols = [
    { id: 'ts', header: 'Time', cell: (c: LedgerCall) => epochToLocal(c.ts), width: 150 },
    ...(props.showUser
      ? [{ id: 'user', header: 'User', cell: (c: LedgerCall) => props.userCell?.(c) ?? c.email ?? c.sub ?? '–' }]
      : []),
    { id: 'model', header: 'Model', cell: (c: LedgerCall) => c.model ?? '–' },
    { id: 'lane', header: 'Lane', cell: (c: LedgerCall) => <Badge color="grey">{c.lane ?? '–'}</Badge>, width: 130 },
    {
      id: 'routing',
      header: 'Routing',
      // derived per request (Req 7.10); a rate_fallback flag means the exact
      // request shape had no published rate and a documented substitution priced it
      cell: (c: LedgerCall) =>
        c.routing ? (
          <>
            <Badge color={c.routing === 'global' ? 'blue' : 'grey'}>{c.routing.replace('_', '-')}</Badge>
            {c.rate_fallback && (
              <Box display="inline" margin={{ left: 'xxs' }}>
                <StatusIndicator type="warning">
                  {c.matched_routing ? `priced ${c.matched_routing.replace('_', '-')}` : 'rate fallback'}
                </StatusIndicator>
              </Box>
            )}
          </>
        ) : (
          '–'
        ),
      width: 150,
    },
    {
      id: 'tok',
      header: 'Tokens in / out',
      cell: (c: LedgerCall) => `${tokens(c.tokens_in)} / ${tokens(c.tokens_out)}`,
      width: 140,
    },
    {
      id: 'usd',
      header: 'Cost',
      cell: (c: LedgerCall) =>
        c.unpriced ? (
          <Box textAlign="right" color="text-status-inactive">
            <StatusIndicator type="info" colorOverride="grey">
              {c.usd_estimate ? `~${usd(c.usd_estimate, 4)} est.` : 'unpriced'}
            </StatusIndicator>
          </Box>
        ) : (
          <Box textAlign="right">{usd(c.usd, 4)}</Box>
        ),
      width: 140,
    },
    {
      id: 'state',
      header: 'State',
      cell: (c: LedgerCall) => (
        <Badge color={c.state === 'SETTLED' ? 'green' : c.state === 'SETTLED_AT_ESTIMATE' ? 'blue' : 'grey'}>
          {c.state ?? '–'}
        </Badge>
      ),
      width: 150,
    },
  ];
  return (
    <Table
      variant="embedded"
      items={props.calls}
      columnDefinitions={cols}
      loading={props.loading}
      loadingText="Loading calls"
      empty={<Box textAlign="center" color="inherit" padding="m">{props.empty ?? 'No calls recorded'}</Box>}
      trackBy={(c) => `${c.ts}#${c.model}#${c.usd}`}
    />
  );
}
