// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Teams & groups: billing-group rollups (async ceilings) + group policies.
// Honest about semantics: rollups are advisory; chargeback comes from the
// ledger / Cost Explorer (docs/METERING.md).

import {
  Alert,
  Box,
  Button,
  ContentLayout,
  Flashbar,
  FormField,
  Header,
  Select,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { useState } from 'react';
import { useModule } from '../App';
import { PolicyModal, type PolicyTarget } from '../components/PolicyModal';
import { useApiData } from '../components/useApiData';
import { recentWindows, tokens, usd } from '../format';
import type { GroupRow, Policy } from '../types';

export default function GroupsPage() {
  const module = useModule();
  const [windowSel, setWindowSel] = useState(module.window);
  const { data, loading, error, refresh } = useApiData<{
    groups: GroupRow[];
    group_policies: Record<string, Policy>;
  }>(`/groups?window=${windowSel}`);
  const [selected, setSelected] = useState<GroupRow[]>([]);
  const [policyTarget, setPolicyTarget] = useState<PolicyTarget | null>(null);
  const [flash, setFlash] = useState<{ type: 'success'; content: string } | null>(null);

  const sel = selected[0];
  const rows = data?.groups ?? [];

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Spend rolled up by billing group (each user's highest-precedence Cognito group). Group ceilings are advisory alerts — per-user hard limits do the blocking; exact chargeback comes from the ledger and Cost Explorer."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={refresh}>Refresh</Button>
              <Button
                disabled={!sel}
                onClick={() =>
                  sel &&
                  setPolicyTarget({
                    scope: `GROUP#${sel.group}`,
                    label: `team ${sel.group}`,
                    existing: data?.group_policies?.[sel.group],
                  })
                }
              >
                Set team ceiling
              </Button>
            </SpaceBetween>
          }
        >
          Teams &amp; groups
        </Header>
      }
    >
      <SpaceBetween size="m">
        {flash && <Flashbar items={[{ ...flash, dismissible: true, onDismiss: () => setFlash(null), id: 'g-flash' }]} />}
        {error && <Alert type="error">Failed to load groups: {error.message}</Alert>}

        <FormField label="Window">
          <Select
            selectedOption={{ label: windowSel, value: windowSel }}
            onChange={({ detail }) => setWindowSel(detail.selectedOption.value!)}
            options={recentWindows().map((w) => ({ label: w, value: w }))}
          />
        </FormField>

        <Table
          header={<Header counter={rows.length ? `(${rows.length})` : undefined}>Billing groups — {windowSel}</Header>}
          items={rows}
          loading={loading}
          loadingText="Loading group rollups"
          selectionType="single"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems as GroupRow[])}
          trackBy="group"
          columnDefinitions={[
            { id: 'group', header: 'Group', cell: (g: GroupRow) => <b>{g.group}</b>, minWidth: 160 },
            { id: 'spend', header: 'Settled spend', cell: (g) => <Box textAlign="right">{usd(g.used_usd ?? g.total_usd)}</Box>, width: 130 },
            {
              id: 'ceiling',
              header: 'Team ceiling',
              cell: (g) =>
                g.ceiling_usd ? (
                  <Box textAlign="right">
                    {usd(g.ceiling_usd)}{' '}
                    {g.pct_of_ceiling != null && (
                      <Box variant="span" color={g.pct_of_ceiling >= 100 ? 'text-status-error' : g.pct_of_ceiling >= 80 ? 'text-status-warning' : 'text-body-secondary'}>
                        ({g.pct_of_ceiling.toFixed(0)}%)
                      </Box>
                    )}
                  </Box>
                ) : (
                  <Box color="text-status-inactive" textAlign="right">none set</Box>
                ),
              width: 170,
            },
            { id: 'tokens', header: 'Tokens in / out', cell: (g) => `${tokens(g.used_in)} / ${tokens(g.used_out)}`, width: 160 },
            { id: 'calls', header: 'Calls', cell: (g) => <Box textAlign="right">{g.req_count ?? 0}</Box>, width: 90 },
          ]}
          empty={
            <Box textAlign="center" padding="xl">
              <b>No group activity this window</b>
              <Box variant="p" color="inherit">
                Rollups build from settled calls (a few seconds behind). Users outside every configured
                billing group appear as “unassigned”.
              </Box>
            </Box>
          }
        />
      </SpaceBetween>

      <PolicyModal
        target={policyTarget}
        onDismiss={() => setPolicyTarget(null)}
        onSaved={() => {
          setFlash({ type: 'success', content: 'Team ceiling saved.' });
          refresh();
        }}
      />
    </ContentLayout>
  );
}
