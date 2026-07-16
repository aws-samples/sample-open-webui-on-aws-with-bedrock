// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Self-service: any authenticated pool user sees their own consumption,
// limits, and recent calls (D7). Answers "why was I blocked?" without a
// support ticket. Admin routes stay 403 for non-admins server-side.

import {
  Box,
  ColumnLayout,
  Container,
  ContentLayout,
  Flashbar,
  Header,
  ProgressBar,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import { useEffect, useState } from 'react';
import { api } from '../api';
import { LedgerTable } from '../components/LedgerTable';
import { tokens, usd } from '../format';
import type { LedgerCall, UsageMe } from '../types';

export default function MyUsagePage() {
  const [me, setMe] = useState<UsageMe | null>(null);
  const [calls, setCalls] = useState<LedgerCall[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<UsageMe>('/usage/me').then(setMe).catch((e: Error) => setErr(e.message));
    api
      .get<{ calls: LedgerCall[] }>('/user/me/ledger?limit=25')
      .then((d) => setCalls(d.calls))
      .catch(() => setCalls([]));
  }, []);

  if (err) {
    return (
      <ContentLayout header={<Header variant="h1">My usage</Header>}>
        <Flashbar items={[{ type: 'error', content: err, id: 'me-err' }]} />
      </ContentLayout>
    );
  }
  if (!me) {
    return (
      <ContentLayout header={<Header variant="h1">My usage</Header>}>
        <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>
      </ContentLayout>
    );
  }

  const used = Number(me.counter.used_usd ?? 0) + Math.max(0, Number(me.counter.est_usd ?? 0));
  const eff = me.resolved?.effective ?? me.policy;
  const hard = Number(eff?.hard_limit_usd ?? 0);
  const pct = hard > 0 ? (100 * used) / hard : 0;

  return (
    <ContentLayout
      header={
        <Header variant="h1" description={`Monthly window ${me.window} — resets on the 1st (UTC).`}>
          My usage
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Container header={<Header variant="h2">AI budget</Header>}>
          <SpaceBetween size="m">
            <ProgressBar
              value={Math.min(pct, 100)}
              status={pct >= 100 ? 'error' : undefined}
              label={`${usd(used)} of ${usd(hard)} used`}
              description={`${pct.toFixed(1)}% of your monthly limit`}
              additionalInfo={
                pct >= 100
                  ? 'You have reached your monthly limit — new requests are blocked until the window resets or an administrator raises your limit.'
                  : pct >= 80
                    ? 'You are approaching your monthly limit.'
                    : undefined
              }
            />
            <ColumnLayout columns={4} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Calls</Box>
                <Box>{Number(me.counter.req_count ?? 0)}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Tokens in</Box>
                <Box>{tokens(Number(me.counter.used_in ?? 0))}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Tokens out</Box>
                <Box>{tokens(Number(me.counter.used_out ?? 0))}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Rate limit</Box>
                <Box>{eff?.rpm_limit ?? '–'} req/min</Box>
              </div>
            </ColumnLayout>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2" counter={calls ? `(${calls.length})` : undefined}>Recent calls</Header>}>
          <LedgerTable calls={calls ?? []} loading={calls === null} empty="No calls this window" />
        </Container>

        <Box color="text-body-secondary" fontSize="body-s">
          Costs are provider-reported token counts priced from the deployment's price map. Questions
          about your limit go to your administrator.
        </Box>
      </SpaceBetween>
    </ContentLayout>
  );
}
