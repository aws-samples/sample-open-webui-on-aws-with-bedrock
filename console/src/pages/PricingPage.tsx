// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Model Pricing Catalog: every model's effective per-token rate, its source
// (AWS-published / operator override / unpriced), and the controls to override,
// revert, or refresh from the AWS Price List. Answers "why is this $0 / is this
// rate current" for finance (docs/plans/metering-admin-console/02-PRICING-INVESTIGATION.md).

import {
  Alert,
  Badge,
  Box,
  Button,
  ContentLayout,
  Flashbar,
  Header,
  Modal,
  Form,
  FormField,
  Input,
  Pagination,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { useMemo, useState } from 'react';
import { api, ApiError } from '../api';
import { useApiData } from '../components/useApiData';
import { ago } from '../format';
import type { PriceRow, PricingCatalog } from '../types';

const PAGE = 25;

/** Per-token USD renders tiny; show per-1M tokens (how AWS quotes) for readability. */
function perM(v: number | null | undefined): string {
  if (v === undefined || v === null) return '–';
  return `$${(v * 1_000_000).toLocaleString('en-US', { maximumFractionDigits: 2 })}/M`;
}

function SourceBadge({ source }: { source: PriceRow['effective']['source'] }) {
  if (source === 'override') return <StatusIndicator type="info">override</StatusIndicator>;
  if (source === 'aws-published') return <StatusIndicator type="success">AWS published</StatusIndicator>;
  if (source === 'default-override') return <StatusIndicator type="in-progress" colorOverride="blue">default (est.)</StatusIndicator>;
  return <StatusIndicator type="warning">unpriced</StatusIndicator>;
}

export default function PricingPage() {
  const { data, loading, error, refresh } = useApiData<PricingCatalog>('/pricing');
  const [filterText, setFilterText] = useState('');
  const [showUnpriced, setShowUnpriced] = useState(false);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<PriceRow[]>([]);
  const [editing, setEditing] = useState<PriceRow | null>(null);
  const [flash, setFlash] = useState<{ type: 'success' | 'error'; content: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const rows = data?.models ?? [];
  const filtered = useMemo(() => {
    let r = rows;
    if (showUnpriced) r = r.filter((m) => m.effective.source === 'unpriced');
    const q = filterText.trim().toLowerCase();
    if (q) r = r.filter((m) => m.model.toLowerCase().includes(q) || (m.provider || '').toLowerCase().includes(q) || (m.display_name || '').toLowerCase().includes(q));
    return r;
  }, [rows, filterText, showUnpriced]);
  const visible = filtered.slice((page - 1) * PAGE, page * PAGE);
  const sel = selected[0];

  const doRefresh = async () => {
    setRefreshing(true);
    setFlash(null);
    try {
      const res = await api.post<{ ok?: boolean; models?: number; version?: string; error?: string }>('/pricing/refresh', {});
      if (res.error) setFlash({ type: 'error', content: res.error });
      else setFlash({ type: 'success', content: `Refreshed from AWS Price List — ${res.models} models priced (version ${res.version}).` });
      refresh();
    } catch (e) {
      setFlash({ type: 'error', content: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setRefreshing(false);
    }
  };

  const revert = async (model: string) => {
    try {
      await api.del(`/pricing/${encodeURIComponent(model)}`);
      setFlash({ type: 'success', content: `Override removed — ${model} reverts to the AWS-published rate (or unpriced).` });
      setSelected([]);
      refresh();
    } catch (e) {
      setFlash({ type: 'error', content: e instanceof ApiError ? e.message : String(e) });
    }
  };

  const counts = useMemo(() => {
    const c = { published: 0, override: 0, default: 0, unpriced: 0 };
    for (const m of rows) {
      const s = m.effective.source;
      if (s === 'aws-published') c.published++;
      else if (s === 'default-override') c.default++;
      else if (s === 'override') c.override++;
      else c.unpriced++;
    }
    return c;
  }, [rows]);

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={
            data?.meta?.refreshed_at
              ? `Effective per-model rates. Default is AWS-published pricing (last refreshed ${ago(data.meta.refreshed_at)}, version ${data.meta.version ?? '—'}); operator overrides win. Rates shown per 1M tokens.`
              : 'Effective per-model rates. Default is AWS-published pricing; operator overrides win. Rates shown per 1M tokens.'
          }
          counter={rows.length ? `(${counts.published} published · ${counts.override} override · ${counts.default} default est. · ${counts.unpriced} unpriced)` : undefined}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="refresh" onClick={refresh}>Reload</Button>
              <Button iconName="download" loading={refreshing} onClick={() => void doRefresh()}>
                Refresh from AWS
              </Button>
              <Button disabled={!sel} onClick={() => sel && setEditing(sel)}>
                {sel?.override ? 'Edit override' : 'Set override'}
              </Button>
              <Button
                disabled={!sel?.override}
                onClick={() => sel && void revert(sel.model)}
              >
                Revert to AWS
              </Button>
            </SpaceBetween>
          }
        >
          Model pricing catalog
        </Header>
      }
    >
      <SpaceBetween size="m">
        {flash && <Flashbar items={[{ ...flash, dismissible: true, onDismiss: () => setFlash(null), id: 'p-flash' }]} />}
        {error && <Alert type="error">Failed to load pricing: {error.message}</Alert>}
        {counts.unpriced > 0 && (
          <Alert type="warning" header={`${counts.unpriced} model(s) are unpriced`}>
            These models have no AWS-published mantle SKU (typically pre-GA/frontier versions). Usage is
            metered in tokens and shown as "unpriced" — set an operator override to bring them under
            dollar accounting rather than leaving them at $0.
          </Alert>
        )}

        <Table
          items={visible}
          loading={loading}
          loadingText="Loading pricing catalog"
          selectionType="single"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems as PriceRow[])}
          trackBy="model"
          variant="container"
          filter={
            <SpaceBetween direction="horizontal" size="xs">
              <TextFilter
                filteringText={filterText}
                filteringPlaceholder="Find model or provider"
                onChange={({ detail }) => { setFilterText(detail.filteringText); setPage(1); }}
                countText={`${filtered.length} match`}
              />
              <Button
                variant={showUnpriced ? 'primary' : 'normal'}
                onClick={() => { setShowUnpriced((v) => !v); setPage(1); }}
              >
                {showUnpriced ? 'Showing unpriced' : 'Unpriced only'}
              </Button>
            </SpaceBetween>
          }
          pagination={
            <Pagination
              currentPageIndex={page}
              pagesCount={Math.max(1, Math.ceil(filtered.length / PAGE))}
              onChange={({ detail }) => setPage(detail.currentPageIndex)}
            />
          }
          columnDefinitions={[
            { id: 'model', header: 'Model', cell: (m: PriceRow) => <Box variant="samp">{m.model}</Box>, minWidth: 240, sortingField: 'model' },
            { id: 'provider', header: 'Provider', cell: (m) => m.provider || '–', width: 130 },
            { id: 'in', header: 'Input', cell: (m) => <Box textAlign="right">{perM(m.effective.input)}</Box>, width: 130 },
            { id: 'out', header: 'Output', cell: (m) => <Box textAlign="right">{perM(m.effective.output)}</Box>, width: 130 },
            { id: 'source', header: 'Source', cell: (m) => <SourceBadge source={m.effective.source} />, width: 160 },
            {
              id: 'note',
              header: 'Note',
              cell: (m) =>
                m.override?.note ? <Box fontSize="body-s">{m.override.note}</Box>
                : m.effective.source === 'default-override' && m.default?.note ? <Box fontSize="body-s" color="text-body-secondary">{m.default.note}</Box>
                : m.override ? <Badge>custom</Badge> : '–',
              minWidth: 200,
            },
          ]}
          empty={
            <Box textAlign="center" padding="xl">
              <b>No pricing rows yet</b>
              <Box variant="p" color="inherit">Click "Refresh from AWS" to populate the catalog from the AWS Price List.</Box>
            </Box>
          }
        />
      </SpaceBetween>

      <OverrideModal
        row={editing}
        onDismiss={() => setEditing(null)}
        onSaved={() => {
          setFlash({ type: 'success', content: 'Override saved. New rate applies to the next settled call (≤5 min cache).' });
          setEditing(null);
          refresh();
        }}
      />
    </ContentLayout>
  );
}

function OverrideModal(props: { row: PriceRow | null; onDismiss: () => void; onSaved: () => void }) {
  const { row, onDismiss, onSaved } = props;
  const [inp, setInp] = useState('');
  const [out, setOut] = useState('');
  const [note, setNote] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useMemo(() => {
    if (row) {
      // pre-fill with the current effective rate expressed per 1M tokens (easier to type)
      setInp(row.override?.input != null ? String(row.override.input * 1e6) : row.effective.input != null ? String(row.effective.input * 1e6) : '');
      setOut(row.override?.output != null ? String(row.override.output * 1e6) : row.effective.output != null ? String(row.effective.output * 1e6) : '');
      setNote(row.override?.note ?? '');
      setErr(null);
    }
  }, [row]);

  if (!row) return null;

  const save = async () => {
    setErr(null);
    const body: Record<string, unknown> = {};
    const parseM = (s: string): number | null => {
      if (s.trim() === '') return null;
      const perMillion = parseFloat(s);
      if (Number.isNaN(perMillion) || perMillion < 0) return NaN as unknown as number;
      return perMillion / 1e6; // store per-token
    };
    const pin = parseM(inp);
    const pout = parseM(out);
    if (Number.isNaN(pin as number) || Number.isNaN(pout as number)) {
      setErr('Rates must be non-negative numbers (USD per 1M tokens).');
      return;
    }
    if (pin === null && pout === null) {
      setErr('Enter at least an input or output rate.');
      return;
    }
    if (pin !== null) body.input = pin;
    if (pout !== null) body.output = pout;
    if (note.trim()) body.note = note.trim();

    setSaving(true);
    try {
      await api.put(`/pricing/${encodeURIComponent(row.model)}`, body);
      onSaved();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Override pricing — ${row.model}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={saving}>Cancel</Button>
            <Button variant="primary" onClick={() => void save()} loading={saving}>Save override</Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form>
        <SpaceBetween size="m">
          {err && <Alert type="error">{err}</Alert>}
          <Box variant="p" color="text-body-secondary">
            Rates are USD per <b>1M tokens</b> (how AWS quotes them); stored internally per token. This
            override wins over the AWS-published rate for {row.model} and is audited.
            {row.effective.source === 'aws-published' && (
              <> Current AWS-published: input {perM(row.effective.input)}, output {perM(row.effective.output)}.</>
            )}
          </Box>
          <FormField label="Input rate (USD per 1M tokens)">
            <Input value={inp} onChange={({ detail }) => setInp(detail.value)} type="number" inputMode="decimal" placeholder="e.g. 3.00" />
          </FormField>
          <FormField label="Output rate (USD per 1M tokens)">
            <Input value={out} onChange={({ detail }) => setOut(detail.value)} type="number" inputMode="decimal" placeholder="e.g. 15.00" />
          </FormField>
          <FormField label="Note — optional" description="Why this custom rate (shown in the catalog + audit trail).">
            <Input value={note} onChange={({ detail }) => setNote(detail.value)} placeholder="e.g. negotiated rate / pre-GA estimate pending AWS publish" />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}
