// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// User drill-in: profile + effective policy (with the resolution chain made
// visible), consumption, open estimates, and paged call history.

import {
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ContentLayout,
  Flashbar,
  Header,
  Popover,
  ProgressBar,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import { useContext, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '../api';
import { SelfContext, useModule } from '../App';
import { LedgerTable } from '../components/LedgerTable';
import { PolicyModal, type PolicyTarget } from '../components/PolicyModal';
import { ResetCounterModal } from '../components/ResetCounterModal';
import { ago, epochToLocal, tokens, usd } from '../format';
import type { LedgerCall, UserDetail } from '../types';

export default function UserDetailPage() {
  const { sub = '' } = useParams();
  const module = useModule();
  const self = useContext(SelfContext);
  const [detail, setDetail] = useState<UserDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [calls, setCalls] = useState<LedgerCall[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [callsLoading, setCallsLoading] = useState(false);
  const [policyTarget, setPolicyTarget] = useState<PolicyTarget | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [flash, setFlash] = useState<{ type: 'success' | 'error'; content: string } | null>(null);

  const load = () => {
    setErr(null);
    api
      .get<UserDetail>(`/user/${encodeURIComponent(sub)}`)
      .then((d) => {
        setDetail(d);
        setCalls(d.recent_calls);
      })
      .catch((e: Error) => setErr(e.message));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [sub]);

  const loadMore = () => {
    setCallsLoading(true);
    api
      .get<{ calls: LedgerCall[]; cursor: string | null }>(
        `/user/${encodeURIComponent(sub)}/ledger?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
      )
      .then((d) => {
        setCalls((prev) => (cursor ? [...prev, ...d.calls] : d.calls));
        setCursor(d.cursor);
      })
      .catch((e: Error) => setFlash({ type: 'error', content: `Ledger load failed: ${e.message}` }))
      .finally(() => setCallsLoading(false));
  };

  if (err) {
    return (
      <ContentLayout header={<Header variant="h1">User</Header>}>
        <Flashbar items={[{ type: 'error', content: err, id: 'ud-err' }]} />
      </ContentLayout>
    );
  }
  if (!detail) {
    return (
      <ContentLayout header={<Header variant="h1">User</Header>}>
        <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>
      </ContentLayout>
    );
  }

  const isSelf = sub === self;
  const eff = detail.policy.effective;
  const hard = Number(eff.hard_limit_usd ?? 0);
  const label = detail.profile.email || sub;
  const overrideUntil = eff.override_until ? Number(eff.override_until) : undefined;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={
            <SpaceBetween direction="horizontal" size="xs">
              <Box variant="span" color="text-body-secondary">{sub}</Box>
              {detail.groups.map((g) => <Badge key={g} color={detail.is_admin_group_member && module.admin_groups.includes(g) ? 'red' : 'grey'}>{g}</Badge>)}
              {detail.profile.status === 'NOT_FOUND' && <Badge color="red">deleted from pool</Badge>}
            </SpaceBetween>
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setPolicyTarget({ scope: `USER#${sub}`, label, existing: detail.policy.chain.user_override ?? undefined })}>
                Set limits
              </Button>
              <Button onClick={() => setPolicyTarget({ scope: `USER#${sub}`, label, existing: detail.policy.chain.user_override ?? undefined, timeboxDefault: true })}>
                Grant override
              </Button>
              <Button disabled={isSelf} onClick={() => setResetOpen(true)}>Reset counter</Button>
            </SpaceBetween>
          }
        >
          {label}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {flash && <Flashbar items={[{ ...flash, dismissible: true, onDismiss: () => setFlash(null), id: 'ud-flash' }]} />}
        {isSelf && (
          <Flashbar
            items={[{
              type: 'info',
              content: 'This is your own account — the four-eyes rule disables resets and self-edits here.',
              id: 'self-note',
            }]}
          />
        )}

        <ColumnLayout columns={2}>
          <Container header={<Header variant="h2">Consumption — {detail.window}</Header>}>
            <SpaceBetween size="m">
              <ProgressBar
                value={Math.min(detail.pct_of_limit, 100)}
                status={detail.pct_of_limit >= 100 ? 'error' : undefined}
                label={`${usd(detail.total_usd)} of ${usd(hard)}`}
                additionalInfo={
                  detail.pct_of_limit >= 100
                    ? 'Over limit — new requests are blocked until a reset, a higher limit, or the window reset.'
                    : detail.pct_of_limit >= 80
                      ? 'Approaching limit — the user has seen a warning toast.'
                      : undefined
                }
                description={`${detail.pct_of_limit.toFixed(1)}% of the hard limit`}
              />
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Settled</Box>
                  <Box>{usd(Number(detail.counter.used_usd ?? 0))}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Reserved (in-flight)</Box>
                  <Box>{usd(Math.max(0, Number(detail.counter.est_usd ?? 0)))}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Calls</Box>
                  <Box>{Number(detail.counter.req_count ?? 0)}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Tokens in</Box>
                  <Box>{tokens(Number(detail.counter.used_in ?? 0))}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Tokens out</Box>
                  <Box>{tokens(Number(detail.counter.used_out ?? 0))}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Last activity</Box>
                  <Box>{ago(Number(detail.counter.updated_at ?? 0) || undefined)}</Box>
                </div>
              </ColumnLayout>
            </SpaceBetween>
          </Container>

          <Container
            header={
              <Header
                variant="h2"
                info={
                  <Popover
                    header="How limits resolve"
                    content="A user-specific override wins; otherwise the explicit DEFAULT policy; otherwise the deployment's environment defaults. Group policies are team ceilings (see Teams & groups), not per-user limits."
                  >
                    <Button variant="inline-link" iconName="status-info" ariaLabel="policy resolution help" />
                  </Popover>
                }
              >
                Effective policy
              </Header>
            }
          >
            <SpaceBetween size="s">
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Hard limit</Box>
                  <Box fontWeight="bold">{usd(hard)}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Warn at</Box>
                  <Box>{usd(Number(eff.soft_limit_usd ?? 0))}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Rate limit</Box>
                  <Box>{eff.rpm_limit ?? '–'} req/min</Box>
                </div>
              </ColumnLayout>
              <div>
                <Box variant="awsui-key-label">Source</Box>
                <Badge color={detail.policy.source.startsWith('USER#') ? 'blue' : 'grey'}>
                  {detail.policy.source.startsWith('USER#') ? 'user override' : detail.policy.source}
                </Badge>
                {overrideUntil && (
                  <Box variant="span" padding={{ left: 's' }} color={overrideUntil * 1000 < Date.now() ? 'text-status-error' : 'text-body-secondary'}>
                    {overrideUntil * 1000 < Date.now() ? 'expired ' : 'expires '}
                    {epochToLocal(overrideUntil)}
                  </Box>
                )}
              </div>
              {eff.note && (
                <div>
                  <Box variant="awsui-key-label">Note</Box>
                  <Box>{String(eff.note)}</Box>
                </div>
              )}
              {eff.updated_by && (
                <Box color="text-body-secondary" fontSize="body-s">
                  Last changed by {String(eff.updated_by)} · {epochToLocal(Number(eff.updated_at))}
                </Box>
              )}
            </SpaceBetween>
          </Container>
        </ColumnLayout>

        {detail.open_estimates.length > 0 && (
          <Container
            header={
              <Header
                variant="h2"
                counter={`(${detail.open_estimates.length})`}
                description="Admission reservations awaiting settlement — refunded automatically after 15 minutes if the stream never settles."
              >
                In-flight reservations
              </Header>
            }
          >
            <SpaceBetween size="xs">
              {detail.open_estimates.map((e) => (
                <Box key={e.estimate_key}>
                  <SpaceBetween direction="horizontal" size="s">
                    <Badge color="blue">{usd(e.usd, 4)}</Badge>
                    <Box variant="span">{e.model ?? 'unknown model'}</Box>
                    <Box variant="span" color="text-body-secondary">{e.lane}</Box>
                    <Box variant="span" color="text-body-secondary">{ago(e.created_at)}</Box>
                  </SpaceBetween>
                </Box>
              ))}
            </SpaceBetween>
          </Container>
        )}

        <Container
          header={
            <Header
              variant="h2"
              counter={`(${calls.length}${cursor ? '+' : ''})`}
              actions={
                <Button onClick={loadMore} loading={callsLoading} disabled={!cursor && calls.length > 10}>
                  {cursor ? 'Load more' : 'Load full history'}
                </Button>
              }
            >
              Call history
            </Header>
          }
        >
          <LedgerTable calls={calls} loading={callsLoading && calls.length === 0} empty="No settled calls for this user" />
        </Container>
      </SpaceBetween>

      <PolicyModal
        target={policyTarget}
        onDismiss={() => setPolicyTarget(null)}
        onSaved={() => {
          setFlash({ type: 'success', content: 'Policy saved. New limits apply to the next request (≤60 s cache).' });
          load();
        }}
      />
      <ResetCounterModal
        target={resetOpen ? { sub, label, window: detail.window, totalUsd: detail.total_usd } : null}
        onDismiss={() => setResetOpen(false)}
        onDone={() => {
          setFlash({ type: 'success', content: 'Counter reset. The user can chat again immediately.' });
          load();
        }}
      />
    </ContentLayout>
  );
}
