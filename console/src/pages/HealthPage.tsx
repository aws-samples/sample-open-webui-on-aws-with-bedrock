// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Module health: the metering plane's own alarms, canaries, degraded-check
// and sweeper telemetry, open estimates, and alert subscriptions.

import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ContentLayout,
  Flashbar,
  FormField,
  Header,
  Input,
  LineChart,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { useMemo, useState } from 'react';
import { api, ApiError } from '../api';
import { useApiData } from '../components/useApiData';
import { ago, usd } from '../format';
import type { Alarm, AlertSubscription, MetricsResponse, OpenEstimate } from '../types';

export default function HealthPage() {
  const alarms = useApiData<{ alarms: Alarm[] }>('/alarms');
  const metrics = useApiData<MetricsResponse>('/metrics?range=24h');
  const estimates = useApiData<{ estimates: OpenEstimate[] }>('/estimates');
  const subs = useApiData<{ subscriptions: AlertSubscription[] }>('/alert-subscriptions');
  const [email, setEmail] = useState('');
  const [flash, setFlash] = useState<{ type: 'success' | 'error'; content: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const degraded = useMemo(() => toSeries(metrics.data, 'degraded_checks'), [metrics.data]);
  const swept = useMemo(() => toSeries(metrics.data, 'swept_usd'), [metrics.data]);

  const subscribe = async () => {
    setBusy(true);
    setFlash(null);
    try {
      await api.post('/alert-subscriptions', { email: email.trim() });
      setFlash({ type: 'success', content: `Subscription created — ${email.trim()} must confirm via the SNS email.` });
      setEmail('');
      subs.refresh();
    } catch (e) {
      setFlash({ type: 'error', content: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const unsubscribe = async (arn: string) => {
    setBusy(true);
    try {
      await api.del(`/alert-subscriptions?arn=${encodeURIComponent(arn)}`);
      setFlash({ type: 'success', content: 'Subscription removed.' });
      subs.refresh();
    } catch (e) {
      setFlash({ type: 'error', content: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Is the metering plane itself healthy? Canaries prove both failure directions hourly; degraded checks mean fail-open grace is being spent."
          actions={
            <Button iconName="refresh" onClick={() => { alarms.refresh(); metrics.refresh(); estimates.refresh(); subs.refresh(); }}>
              Refresh
            </Button>
          }
        >
          Module health
        </Header>
      }
    >
      <SpaceBetween size="l">
        {flash && <Flashbar items={[{ ...flash, dismissible: true, onDismiss: () => setFlash(null), id: 'h-flash' }]} />}

        <Container header={<Header variant="h2">Alarms</Header>}>
          <Table
            variant="embedded"
            items={alarms.data?.alarms ?? []}
            loading={alarms.loading}
            loadingText="Loading alarms"
            trackBy="name"
            columnDefinitions={[
              {
                id: 'state',
                header: 'State',
                cell: (a: Alarm) => (
                  <StatusIndicator type={a.state === 'OK' ? 'success' : a.state === 'ALARM' ? 'error' : 'pending'}>
                    {a.state}
                  </StatusIndicator>
                ),
                width: 170,
              },
              { id: 'name', header: 'Alarm', cell: (a) => a.name, minWidth: 260 },
              { id: 'metric', header: 'Metric', cell: (a) => <Badge color="grey">{a.metric}</Badge>, width: 220 },
              { id: 'reason', header: 'Reason', cell: (a) => <Box fontSize="body-s">{a.reason}</Box>, minWidth: 240 },
            ]}
            empty={<Box textAlign="center" padding="l">No module alarms found</Box>}
          />
          {(alarms.data?.alarms ?? []).some((a) => a.state === 'INSUFFICIENT_DATA') && (
            <Box padding={{ top: 's' }} color="text-body-secondary" fontSize="body-s">
              INSUFFICIENT_DATA is normal for failure-direction alarms (canary failures, DLQ depth,
              drift) that have never fired — the metric only exists when something goes wrong.
            </Box>
          )}
        </Container>

        <ColumnLayout columns={2}>
          <Container header={<Header variant="h2" description="Fail-open grace being spent — sustained values mean the quota store is unreachable">Degraded checks (24h)</Header>}>
            <LineChart
              series={degraded.length ? [{ title: 'Degraded checks', type: 'line', data: degraded, valueFormatter: (v) => `${v}` }] : []}
              xScaleType="time"
              height={180}
              hideFilter
              hideLegend
              xTitle=""
              yTitle="count"
              statusType={metrics.loading ? 'loading' : 'finished'}
              empty={<Box textAlign="center" padding="l" color="inherit">None in 24h — the enforcement path is healthy</Box>}
            />
          </Container>
          <Container header={<Header variant="h2" description="USD refunded from admission estimates that never settled (aborts, direct callers)">Sweeper refunds (24h)</Header>}>
            <LineChart
              series={swept.length ? [{ title: 'Swept USD', type: 'line', data: swept, valueFormatter: (v) => usd(v, 4) }] : []}
              xScaleType="time"
              height={180}
              hideFilter
              hideLegend
              xTitle=""
              yTitle="USD"
              statusType={metrics.loading ? 'loading' : 'finished'}
              empty={<Box textAlign="center" padding="l" color="inherit">No refunds in 24h</Box>}
            />
          </Container>
        </ColumnLayout>

        <Container
          header={
            <Header
              variant="h2"
              counter={`(${estimates.data?.estimates.length ?? 0})`}
              description="Admission reservations currently in force. Anything older than 15 minutes resolves on the sweeper's next pass."
            >
              Open reservations
            </Header>
          }
        >
          <Table
            variant="embedded"
            items={estimates.data?.estimates ?? []}
            loading={estimates.loading}
            loadingText="Loading estimates"
            trackBy="estimate_key"
            columnDefinitions={[
              { id: 'age', header: 'Age', cell: (e: OpenEstimate) => ago(e.created_at), width: 110 },
              { id: 'sub', header: 'User (sub)', cell: (e) => <Box variant="code">{e.sub}</Box>, minWidth: 220 },
              { id: 'model', header: 'Model', cell: (e) => e.model ?? '–' },
              { id: 'lane', header: 'Lane', cell: (e) => <Badge color="grey">{e.lane ?? '–'}</Badge>, width: 140 },
              { id: 'usd', header: 'Reserved', cell: (e) => <Box textAlign="right">{usd(e.usd, 4)}</Box>, width: 110 },
            ]}
            empty={<Box textAlign="center" padding="l">No open reservations — no requests in flight</Box>}
          />
        </Container>

        <Container
          header={
            <Header variant="h2" description="Email endpoints for quota (80%/100%), canary, DLQ, and drift alerts (SNS).">
              Alert subscriptions
            </Header>
          }
        >
          <SpaceBetween size="m">
            <SpaceBetween direction="horizontal" size="xs">
              <FormField label="Add email endpoint">
                <Input value={email} onChange={({ detail }) => setEmail(detail.value)} placeholder="ops-team@example.com" type="email" />
              </FormField>
              <div style={{ paddingTop: 24 }}>
                <Button variant="primary" onClick={() => void subscribe()} loading={busy} disabled={!email.trim()}>
                  Subscribe
                </Button>
              </div>
            </SpaceBetween>
            <Table
              variant="embedded"
              items={subs.data?.subscriptions ?? []}
              loading={subs.loading}
              loadingText="Loading subscriptions"
              trackBy="arn"
              columnDefinitions={[
                { id: 'endpoint', header: 'Endpoint', cell: (s: AlertSubscription) => s.endpoint, minWidth: 240 },
                { id: 'protocol', header: 'Protocol', cell: (s) => s.protocol, width: 110 },
                {
                  id: 'status',
                  header: 'Status',
                  cell: (s) =>
                    s.pending ? (
                      <StatusIndicator type="pending">Pending confirmation</StatusIndicator>
                    ) : (
                      <StatusIndicator type="success">Confirmed</StatusIndicator>
                    ),
                  width: 200,
                },
                {
                  id: 'act',
                  header: '',
                  cell: (s) =>
                    s.pending ? (
                      <Box color="text-body-secondary" fontSize="body-s">confirm via email</Box>
                    ) : (
                      <Button variant="inline-link" onClick={() => void unsubscribe(s.arn)}>Remove</Button>
                    ),
                  width: 130,
                },
              ]}
              empty={
                <Box textAlign="center" padding="l">
                  <b>Nobody is subscribed to metering alerts</b>
                  <Box variant="p" color="inherit">Add an ops mailbox so quota and canary alerts reach a human.</Box>
                </Box>
              }
            />
          </SpaceBetween>
        </Container>

        <Alert type="info" header="Deeper operations">
          DLQ redrive, chaos drills, and rollback procedures live in docs/METERING.md. The CloudWatch
          dashboard “open-webui-metering” carries the same series with full CloudWatch tooling.
        </Alert>
      </SpaceBetween>
    </ContentLayout>
  );
}

function toSeries(m: MetricsResponse | null, key: string): { x: Date; y: number }[] {
  return (m?.series?.[key] ?? []).map(([t, v]) => ({ x: new Date(t * 1000), y: v }));
}
