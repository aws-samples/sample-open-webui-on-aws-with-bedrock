// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Audit trail: every admin mutation (actor, action, target, before → after).

import {
  Alert,
  Badge,
  Box,
  Button,
  ContentLayout,
  ExpandableSection,
  FormField,
  Header,
  Select,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { useState } from 'react';
import { useApiData } from '../components/useApiData';
import { epochToLocal } from '../format';
import type { AuditEntry } from '../types';

const ACTION_COLOR: Record<string, 'blue' | 'red' | 'green' | 'grey'> = {
  PUT_POLICY: 'blue',
  DELETE_POLICY: 'red',
  OVERRIDE: 'blue',
  COUNTER_RESET: 'green',
  SUBSCRIBE_ALERTS: 'grey',
  UNSUBSCRIBE_ALERTS: 'grey',
};

export default function AuditPage() {
  const [days, setDays] = useState('7');
  const { data, loading, error, refresh } = useApiData<{ entries: AuditEntry[] }>(`/audit?days=${days}`);
  const entries = data?.entries ?? [];

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Every mutation made through this console or the CLI, with before/after state. Audit rows are written by the API itself and retained ~15 months."
          actions={<Button iconName="refresh" onClick={refresh}>Refresh</Button>}
        >
          Audit trail
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">Failed to load audit trail: {error.message}</Alert>}
        <FormField label="Look back">
          <Select
            selectedOption={{ label: `${days} days`, value: days }}
            onChange={({ detail }) => setDays(detail.selectedOption.value!)}
            options={['1', '7', '14', '31'].map((d) => ({ label: `${d} days`, value: d }))}
          />
        </FormField>
        <Table
          items={entries}
          loading={loading}
          loadingText="Loading audit entries"
          trackBy={(e) => `${e.ts}#${e.actor}#${e.action}#${e.target}`}
          columnDefinitions={[
            { id: 'ts', header: 'When', cell: (e: AuditEntry) => epochToLocal(e.ts), width: 150 },
            {
              id: 'action',
              header: 'Action',
              cell: (e) => <Badge color={ACTION_COLOR[e.action] ?? 'grey'}>{e.action}</Badge>,
              width: 160,
            },
            { id: 'target', header: 'Target', cell: (e) => <Box variant="code">{e.target}</Box>, minWidth: 200 },
            { id: 'actor', header: 'Actor (sub)', cell: (e) => <Box variant="code">{e.actor}</Box>, minWidth: 200 },
            {
              id: 'diff',
              header: 'Change',
              cell: (e) => (
                <ExpandableSection headerText="before → after" variant="footer">
                  <Box variant="code">
                    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
                      {JSON.stringify({ before: e.before, after: e.after }, null, 2)}
                    </pre>
                  </Box>
                </ExpandableSection>
              ),
              minWidth: 220,
            },
          ]}
          empty={
            <Box textAlign="center" padding="xl">
              <b>No admin actions in this period</b>
              <Box variant="p" color="inherit">Policy changes, overrides, resets, and alert-subscription changes appear here.</Box>
            </Box>
          }
        />
      </SpaceBetween>
    </ContentLayout>
  );
}
