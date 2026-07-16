// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Dashboard: the one-minute answer to "who is burning budget, how much is
// left, and is the module healthy" (brief §good-looks-like).

import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ContentLayout,
  Grid,
  Header,
  Link,
  LineChart,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  Table,
} from '@cloudscape-design/components';
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { QuotaStatus } from '../components/QuotaBar';
import { useApiData } from '../components/useApiData';
import { ago, usd } from '../format';
import { useModule } from '../App';
import type { Alarm, LedgerCall, MetricsResponse, UserRow } from '../types';

export default function DashboardPage() {
  const module = useModule();
  const navigate = useNavigate();
  const [range, setRange] = useState('24h');
  const users = useApiData<{ users: UserRow[] }>(`/users?limit=100`);
  const metrics = useApiData<MetricsResponse>(`/metrics?range=${range}`);
  const alarms = useApiData<{ alarms: Alarm[] }>('/alarms');
  const activity = useApiData<{ calls: LedgerCall[] }>('/activity?limit=12');

  const rows = users.data?.users ?? [];
  const totals = useMemo(() => {
    const spend = rows.reduce((s, u) => s + (u.total_usd || 0), 0);
    const near = rows.filter((u) => u.pct_of_limit >= 80 && u.pct_of_limit < 100).length;
    const over = rows.filter((u) => u.pct_of_limit >= 100).length;
    return { spend, near, over, active: rows.filter((u) => (u.req_count ?? 0) > 0).length };
  }, [rows]);

  const firing = (alarms.data?.alarms ?? []).filter((a) => a.state === 'ALARM');

  const spendSeries = useMemo(() => toSeries(metrics.data, 'spend_usd'), [metrics.data]);
  const denySeries = useMemo(() => toSeries(metrics.data, 'denies'), [metrics.data]);

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={`Window ${module.window} · enforcement ${module.enforce_mode} · price map ${module.price_map_version}`}
          actions={
            <Button
              iconName="refresh"
              onClick={() => {
                users.refresh();
                metrics.refresh();
                alarms.refresh();
                activity.refresh();
              }}
            >
              Refresh
            </Button>
          }
        >
          Dashboard
        </Header>
      }
    >
      <SpaceBetween size="l">
        {firing.length > 0 && (
          <Alert
            type="error"
            header={`${firing.length} metering alarm${firing.length > 1 ? 's' : ''} firing`}
            action={<Button onClick={() => navigate('/health')}>Open module health</Button>}
          >
            {firing.map((a) => a.name).join(' · ')}
          </Alert>
        )}

        {/* KPI row */}
        <Container>
          <ColumnLayout columns={4} variant="text-grid">
            <Kpi label={`Settled + reserved spend (${module.window})`} loading={users.loading}>
              <Box fontSize="display-l" fontWeight="bold">{usd(totals.spend)}</Box>
            </Kpi>
            <Kpi label="Active users this window" loading={users.loading}>
              <Box fontSize="display-l" fontWeight="bold">{totals.active}</Box>
            </Kpi>
            <Kpi label="Approaching limit (≥80%)" loading={users.loading}>
              <Box fontSize="display-l" fontWeight="bold" color={totals.near ? 'text-status-warning' : undefined}>
                {totals.near}
              </Box>
            </Kpi>
            <Kpi label="Over limit (blocked)" loading={users.loading}>
              <Box fontSize="display-l" fontWeight="bold" color={totals.over ? 'text-status-error' : undefined}>
                {totals.over}
              </Box>
            </Kpi>
          </ColumnLayout>
        </Container>

        {/* charts */}
        <Grid gridDefinition={[{ colspan: { default: 12, m: 7 } }, { colspan: { default: 12, m: 5 } }]}>
          <Container
            header={
              <Header
                actions={
                  <SegmentedControl
                    selectedId={range}
                    onChange={({ detail }) => setRange(detail.selectedId)}
                    options={[
                      { id: '3h', text: '3h' },
                      { id: '24h', text: '24h' },
                      { id: '7d', text: '7d' },
                      { id: '30d', text: '30d' },
                    ]}
                  />
                }
              >
                Spend rate (settled USD)
              </Header>
            }
          >
            <LineChart
              series={spendSeries.length ? [{ title: 'Settled USD', type: 'line', data: spendSeries, valueFormatter: (v) => usd(v, 4) }] : []}
              xScaleType="time"
              height={220}
              hideFilter
              hideLegend
              xTitle=""
              yTitle="USD"
              loadingText="Loading spend"
              statusType={metrics.loading ? 'loading' : metrics.error ? 'error' : 'finished'}
              errorText={metrics.error?.message}
              empty={<ChartEmpty text="No settled spend in this range" />}
              i18nStrings={chartI18n}
            />
          </Container>
          <Container header={<Header>Quota denies (429s)</Header>}>
            <LineChart
              series={denySeries.length ? [{ title: 'Denies', type: 'line', data: denySeries, valueFormatter: (v) => `${v}` }] : []}
              xScaleType="time"
              height={220}
              hideFilter
              hideLegend
              xTitle=""
              yTitle="count"
              loadingText="Loading denies"
              statusType={metrics.loading ? 'loading' : metrics.error ? 'error' : 'finished'}
              errorText={metrics.error?.message}
              empty={<ChartEmpty text="No denies in this range — nobody hit a limit" />}
              i18nStrings={chartI18n}
            />
          </Container>
        </Grid>

        {/* top spenders + activity */}
        <Grid gridDefinition={[{ colspan: { default: 12, m: 7 } }, { colspan: { default: 12, m: 5 } }]}>
          <Container
            header={
              <Header
                counter={rows.length ? `(${Math.min(rows.length, 8)} of ${rows.length})` : undefined}
                actions={<Button onClick={() => navigate('/users')}>View all users</Button>}
              >
                Top spenders
              </Header>
            }
          >
            <Table
              variant="embedded"
              loading={users.loading}
              loadingText="Loading users"
              items={rows.slice(0, 8)}
              trackBy="sub"
              columnDefinitions={[
                {
                  id: 'email',
                  header: 'User',
                  cell: (u: UserRow) => (
                    <Link onFollow={(e) => { e.preventDefault(); navigate(`/users/${u.sub}`); }} href={`/users/${u.sub}`}>
                      {u.email || u.sub}
                    </Link>
                  ),
                },
                { id: 'spend', header: 'Spend', cell: (u: UserRow) => <Box textAlign="right">{usd(u.total_usd)}</Box>, width: 110 },
                { id: 'limit', header: 'Limit', cell: (u: UserRow) => <Box textAlign="right">{usd(u.hard_limit_usd)}</Box>, width: 100 },
                { id: 'status', header: 'Status', cell: (u: UserRow) => <QuotaStatus pct={u.pct_of_limit} />, width: 140 },
              ]}
              empty={
                <Box textAlign="center" padding="l">
                  <b>No usage this window</b>
                  <Box variant="p" color="inherit">
                    Counters appear when users start chatting (or seed demo data to evaluate the console).
                  </Box>
                </Box>
              }
            />
          </Container>
          <Container header={<Header description="Settled calls, most recent first">Live activity</Header>}>
            {activity.loading ? (
              <Box textAlign="center" padding="l"><Spinner /></Box>
            ) : (activity.data?.calls ?? []).length === 0 ? (
              <Box textAlign="center" padding="l" color="inherit">No calls settled today</Box>
            ) : (
              <SpaceBetween size="xs">
                {(activity.data?.calls ?? []).map((c) => (
                  <Box key={`${c.ts}-${c.sub}-${c.usd}`}>
                    <SpaceBetween direction="horizontal" size="xs">
                      <Box color="text-body-secondary" fontSize="body-s">{ago(c.ts)}</Box>
                      <Link
                        fontSize="body-s"
                        onFollow={(e) => { e.preventDefault(); navigate(`/users/${c.sub}`); }}
                        href={`/users/${c.sub}`}
                      >
                        {c.email || c.sub?.slice(0, 12)}
                      </Link>
                      <Box fontSize="body-s">{c.model}</Box>
                      <Badge color="grey">{usd(c.usd, 4)}</Badge>
                    </SpaceBetween>
                  </Box>
                ))}
              </SpaceBetween>
            )}
          </Container>
        </Grid>
      </SpaceBetween>
    </ContentLayout>
  );
}

function Kpi(props: { label: string; loading: boolean; children: React.ReactNode }) {
  return (
    <div>
      <Box variant="awsui-key-label">{props.label}</Box>
      {props.loading ? <Spinner /> : props.children}
    </div>
  );
}

function ChartEmpty(props: { text: string }) {
  return (
    <Box textAlign="center" color="inherit" padding="l">
      {props.text}
    </Box>
  );
}

function toSeries(m: MetricsResponse | null, key: string): { x: Date; y: number }[] {
  return (m?.series?.[key] ?? []).map(([t, v]) => ({ x: new Date(t * 1000), y: v }));
}

const chartI18n = {
  xTickFormatter: (d: Date) =>
    d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', month: undefined }) === ''
      ? ''
      : d.getHours() === 0 && d.getMinutes() === 0
        ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
        : d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }),
};
