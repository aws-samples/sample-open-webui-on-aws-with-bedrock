// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { Badge, Box, Table } from '@cloudscape-design/components';
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
      id: 'tok',
      header: 'Tokens in / out',
      cell: (c: LedgerCall) => `${tokens(c.tokens_in)} / ${tokens(c.tokens_out)}`,
      width: 140,
    },
    {
      id: 'usd',
      header: 'Cost',
      cell: (c: LedgerCall) => <Box textAlign="right">{usd(c.usd, 4)}</Box>,
      width: 110,
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
