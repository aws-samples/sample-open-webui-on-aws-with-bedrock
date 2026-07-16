// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Quota policies: every explicit row (DEFAULT / GROUP# / USER#) + the
// implicit environment default, with create/edit/delete.

import {
  Alert,
  Badge,
  Box,
  Button,
  ContentLayout,
  Flashbar,
  Header,
  Modal,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { useContext, useState } from 'react';
import { api, ApiError } from '../api';
import { SelfContext, useModule } from '../App';
import { PolicyModal, type PolicyTarget } from '../components/PolicyModal';
import { useApiData } from '../components/useApiData';
import { epochToLocal, usd } from '../format';
import type { Policy } from '../types';

export default function PoliciesPage() {
  const module = useModule();
  const self = useContext(SelfContext);
  const { data, loading, error, refresh } = useApiData<{ policies: Policy[]; implicit_default: Policy }>('/policies');
  const [selected, setSelected] = useState<Policy[]>([]);
  const [policyTarget, setPolicyTarget] = useState<PolicyTarget | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Policy | null>(null);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [flash, setFlash] = useState<{ type: 'success'; content: string } | null>(null);

  const sel = selected[0];
  const explicit = data?.policies ?? [];
  const hasExplicitDefault = explicit.some((p) => p.scope === 'DEFAULT');
  const rows: Policy[] = hasExplicitDefault ? explicit : data ? [...explicit, data.implicit_default] : [];

  const scopeKind = (scope: string) =>
    scope.startsWith('USER#') ? 'user' : scope.startsWith('GROUP#') ? 'group' : 'default';

  const doDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteErr(null);
    try {
      await api.del(`/policy/${encodeURIComponent(deleteTarget.scope)}`);
      setFlash({ type: 'success', content: `Policy ${deleteTarget.scope} removed — that scope now inherits.` });
      setDeleteTarget(null);
      setSelected([]);
      refresh();
    } catch (e) {
      setDeleteErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={`Precedence: user override → default policy → environment defaults (${usd(module.defaults.hard_limit_usd)} hard / ${usd(module.defaults.soft_limit_usd)} warn / ${module.defaults.rpm_limit} rpm). Group policies are team ceilings.`}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={refresh}>Refresh</Button>
              <Button
                disabled={!sel || sel.scope.includes('(environment)')}
                onClick={() => sel && setPolicyTarget({ scope: sel.scope, label: sel.scope, existing: sel })}
              >
                Edit
              </Button>
              <Button
                disabled={!sel || sel.scope.includes('(environment)') || sel.scope === `USER#${self}`}
                onClick={() => sel && setDeleteTarget(sel)}
              >
                Delete
              </Button>
              <Button variant="primary" onClick={() => setPolicyTarget({ scope: 'DEFAULT', label: 'deployment default', existing: rows.find((p) => p.scope === 'DEFAULT') })}>
                Set default policy
              </Button>
            </SpaceBetween>
          }
        >
          Quota policies
        </Header>
      }
    >
      <SpaceBetween size="m">
        {flash && <Flashbar items={[{ ...flash, dismissible: true, onDismiss: () => setFlash(null), id: 'p-flash' }]} />}
        {error && <Alert type="error">Failed to load policies: {error.message}</Alert>}

        <Table
          items={rows}
          loading={loading}
          loadingText="Loading policies"
          selectionType="single"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems as Policy[])}
          isItemDisabled={(p) => p.scope.includes('(environment)')}
          trackBy="scope"
          columnDefinitions={[
            {
              id: 'scope',
              header: 'Scope',
              cell: (p: Policy) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Badge color={scopeKind(p.scope) === 'user' ? 'blue' : scopeKind(p.scope) === 'group' ? 'green' : 'grey'}>
                    {scopeKind(p.scope)}
                  </Badge>
                  <Box variant="span" fontWeight={scopeKind(p.scope) === 'default' ? 'bold' : 'normal'}>
                    {p.scope.replace(/^(USER#|GROUP#)/, '')}
                  </Box>
                </SpaceBetween>
              ),
              minWidth: 260,
            },
            { id: 'hard', header: 'Hard limit', cell: (p) => <Box textAlign="right">{usd(p.hard_limit_usd)}</Box>, width: 110 },
            { id: 'soft', header: 'Warn at', cell: (p) => <Box textAlign="right">{usd(p.soft_limit_usd)}</Box>, width: 100 },
            { id: 'rpm', header: 'Req/min', cell: (p) => <Box textAlign="right">{p.rpm_limit ?? '–'}</Box>, width: 90 },
            {
              id: 'until',
              header: 'Expires',
              cell: (p) =>
                p.override_until ? (
                  <Box color={p.override_until * 1000 < Date.now() ? 'text-status-error' : undefined}>
                    {p.override_until * 1000 < Date.now() ? 'expired ' : ''}
                    {epochToLocal(p.override_until)}
                  </Box>
                ) : (
                  '–'
                ),
              width: 150,
            },
            { id: 'note', header: 'Note', cell: (p) => p.note ?? '–', minWidth: 160 },
            {
              id: 'by',
              header: 'Last change',
              cell: (p) => (p.updated_by ? `${epochToLocal(p.updated_at)}` : '–'),
              width: 150,
            },
          ]}
          empty={<Box textAlign="center" padding="xl">No policies</Box>}
        />
        <Alert type="info" header="Expiry semantics">
          A time-boxed override records its expiry (`until`) so operators can see and audit it; the
          enforcement plane applies the row while present. This page is where expired overrides get
          cleaned up — expired rows are flagged red. Deleting a USER row returns that user to the
          default policy immediately.
        </Alert>
      </SpaceBetween>

      <PolicyModal
        target={policyTarget}
        onDismiss={() => setPolicyTarget(null)}
        onSaved={() => {
          setFlash({ type: 'success', content: 'Policy saved.' });
          refresh();
        }}
      />

      <Modal
        visible={!!deleteTarget}
        onDismiss={() => setDeleteTarget(null)}
        header={`Delete policy — ${deleteTarget?.scope}`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)} disabled={deleting}>Cancel</Button>
              <Button variant="primary" onClick={() => void doDelete()} loading={deleting}>Delete</Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {deleteErr && <Alert type="error">{deleteErr}</Alert>}
          <Box variant="p">
            {deleteTarget?.scope === 'DEFAULT'
              ? `Deleting the DEFAULT policy reverts every non-overridden user to the environment defaults (${usd(module.defaults.hard_limit_usd)} hard).`
              : `The scope inherits from the default policy after deletion. The action is audited.`}
          </Box>
        </SpaceBetween>
      </Modal>
    </ContentLayout>
  );
}
