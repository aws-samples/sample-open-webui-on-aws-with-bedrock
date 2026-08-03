// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Model Pricing Catalog: per-model published rate grids (per routing mode),
// the operator override controls, and the UNMATCHED review queue — Price List
// entries the refresher could not bind to a Bedrock model id, with a
// bind-to-model action that outranks automatic resolution on the next refresh.
// Two sources only: AWS-published and operator override; anything else is
// honestly unpriced (.kiro/specs/metering-pricing-single-source/design.md).

import {
  Alert,
  Badge,
  Box,
  Button,
  ContentLayout,
  ExpandableSection,
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
import type { PriceRow, PricingCatalog, RateGrid, UnmatchedRow } from '../types';

const PAGE = 25;

/** Rates arrive as USD per 1M tokens (how AWS quotes). */
function perM(v: number | null | undefined): string {
  if (v === undefined || v === null) return '–';
  return `$${v.toLocaleString('en-US', { maximumFractionDigits: 2 })}/M`;
}

function gridStd(rates: RateGrid | null | undefined, routing: string, direction: string): number | null {
  const v = rates?.[routing]?.standard?.default?.[direction];
  return typeof v === 'number' ? v : null;
}

function SourceBadge({ source }: { source: PriceRow['effective']['source'] }) {
  if (source === 'override') return <StatusIndicator type="info">override</StatusIndicator>;
  if (source === 'aws-published') return <StatusIndicator type="success">AWS published</StatusIndicator>;
  return <StatusIndicator type="warning">unpriced</StatusIndicator>;
}

export default function PricingPage() {
  const { data, loading, error, refresh } = useApiData<PricingCatalog>('/pricing');
  const [filterText, setFilterText] = useState('');
  const [showUnpriced, setShowUnpriced] = useState(false);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<PriceRow[]>([]);
  const [editing, setEditing] = useState<PriceRow | null>(null);
  const [binding, setBinding] = useState<UnmatchedRow | null>(null);
  const [flash, setFlash] = useState<{ type: 'success' | 'error'; content: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const rows = data?.models ?? [];
  const unmatched = data?.unmatched ?? [];
  const aliases = data?.aliases ?? [];
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
      const res = await api.post<{ ok?: boolean; models?: number; rows?: number; unmatched?: number; partial?: boolean; error?: string }>('/pricing/refresh', {});
      if (res.error) setFlash({ type: 'error', content: res.error });
      else
        setFlash({
          type: 'success',
          content: `Refreshed from the AWS Price List — ${res.models} models priced (${res.rows} keys), ${res.unmatched} unmatched${res.partial ? ' · PARTIAL: an offer file was unavailable' : ''}.`,
        });
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

  const unbind = async (name: string) => {
    try {
      await api.del(`/pricing/alias/${encodeURIComponent(name)}`);
      setFlash({ type: 'success', content: `Alias removed — "${name}" returns to automatic resolution on the next refresh.` });
      refresh();
    } catch (e) {
      setFlash({ type: 'error', content: e instanceof ApiError ? e.message : String(e) });
    }
  };

  const counts = useMemo(() => {
    const c = { published: 0, override: 0, unpriced: 0 };
    for (const m of rows) {
      if (m.effective.source === 'aws-published') c.published++;
      else if (m.effective.source === 'override') c.override++;
      else c.unpriced++;
    }
    return c;
  }, [rows]);

  const hasGlobal = useMemo(() => rows.some((m) => m.routing_modes?.includes('global')), [rows]);

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={
            data?.meta?.refreshed_at
              ? `Published rate grids per routing mode, refreshed from the AWS Price List (last ${ago(data.meta.refreshed_at)}, generation ${data.meta.refresh_generation ?? '—'}); operator overrides win. Rates are USD per 1M tokens.`
              : 'Published rate grids per routing mode; operator overrides win. Rates are USD per 1M tokens.'
          }
          counter={rows.length ? `(${counts.published} AWS · ${counts.override} override · ${counts.unpriced} unpriced · ${unmatched.length} unmatched)` : undefined}
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
        {data?.meta?.partial && (
          <Alert type="warning" header="Last refresh was partial">
            An offer file was unavailable; previously stored rates were kept and stale rows were not
            garbage-collected. Check the PricingRefreshFailure alarm.
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
            {
              id: 'model',
              header: 'Model',
              cell: (m: PriceRow) => (
                <>
                  <Box variant="samp">{m.model}</Box>
                  {m.keys.length > 1 && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      +{m.keys.length - 1} alias key{m.keys.length > 2 ? 's' : ''}
                    </Box>
                  )}
                </>
              ),
              minWidth: 240,
              sortingField: 'model',
            },
            { id: 'provider', header: 'Provider', cell: (m) => m.provider || '–', width: 120 },
            {
              id: 'in',
              header: 'In-region in / out',
              cell: (m) => (
                <Box textAlign="right">
                  {m.effective.source === 'override'
                    ? `${perM(m.effective.input_per_1m)} / ${perM(m.effective.output_per_1m)}`
                    : `${perM(gridStd(m.rates, 'in_region', 'input'))} / ${perM(gridStd(m.rates, 'in_region', 'output'))}`}
                </Box>
              ),
              width: 170,
            },
            ...(hasGlobal
              ? [{
                  id: 'global',
                  header: 'Global in / out',
                  cell: (m: PriceRow) =>
                    m.routing_modes?.includes('global') ? (
                      <Box textAlign="right">
                        {perM(gridStd(m.rates, 'global', 'input'))} / {perM(gridStd(m.rates, 'global', 'output'))}
                      </Box>
                    ) : (
                      <Box textAlign="right" color="text-body-secondary">–</Box>
                    ),
                  width: 160,
                }]
              : []),
            {
              id: 'routing',
              header: 'Routing',
              cell: (m) =>
                m.routing_modes?.length ? (
                  <SpaceBetween direction="horizontal" size="xxs">
                    {m.routing_modes.map((r) => (
                      <Badge key={r} color={r === 'global' ? 'blue' : 'grey'}>{r.replace('_', '-')}</Badge>
                    ))}
                  </SpaceBetween>
                ) : (
                  '–'
                ),
              width: 150,
            },
            { id: 'source', header: 'Source', cell: (m) => <SourceBadge source={m.effective.source} />, width: 140 },
            {
              id: 'note',
              header: 'Note',
              cell: (m) =>
                m.override?.note ? <Box fontSize="body-s">{m.override.note}</Box>
                : m.resolved_via === 'alias' ? <Badge color="blue">operator alias</Badge>
                : m.override ? <Badge>custom</Badge> : '–',
              minWidth: 160,
            },
          ]}
          empty={
            <Box textAlign="center" padding="xl">
              <b>No pricing rows yet</b>
              <Box variant="p" color="inherit">Click "Refresh from AWS" to populate the catalog from the AWS Price List.</Box>
            </Box>
          }
        />

        <ExpandableSection
          headerText={`Unmatched Price List entries (${unmatched.length})`}
          variant="container"
          defaultExpanded={unmatched.length > 0 && unmatched.length <= 10}
          headerDescription="AWS publishes a token rate for these, but no Bedrock model id could be resolved without guessing. Bind a model id to price it on the next refresh — bindings outrank automatic matching."
        >
          <Table
            items={unmatched}
            variant="embedded"
            trackBy="price_list_name"
            columnDefinitions={[
              { id: 'name', header: 'Price List name', cell: (u: UnmatchedRow) => <Box variant="samp">{u.price_list_name}</Box>, minWidth: 220 },
              { id: 'provider', header: 'Provider', cell: (u) => u.provider || '–', width: 120 },
              { id: 'svc', header: 'Offer file', cell: (u) => <Box fontSize="body-s">{u.service_code || '–'}</Box>, width: 220 },
              {
                id: 'reason',
                header: 'Reason',
                cell: (u) => (
                  <StatusIndicator type={u.reason === 'ambiguous-match' ? 'warning' : 'info'}>
                    {u.reason}
                  </StatusIndicator>
                ),
                width: 200,
              },
              {
                id: 'rates',
                header: 'Published in / out',
                cell: (u) => (
                  <Box textAlign="right">
                    {perM(gridStd(u.candidate_rates, 'in_region', 'input') ?? gridStd(u.candidate_rates, 'global', 'input'))}
                    {' / '}
                    {perM(gridStd(u.candidate_rates, 'in_region', 'output') ?? gridStd(u.candidate_rates, 'global', 'output'))}
                  </Box>
                ),
                width: 160,
              },
              {
                id: 'bind',
                header: '',
                cell: (u) => <Button variant="inline-link" onClick={() => setBinding(u)}>Bind to model id</Button>,
                width: 150,
              },
            ]}
            empty={<Box textAlign="center" padding="m" color="inherit">Every published token rate is bound to a model id.</Box>}
          />
          {aliases.length > 0 && (
            <Box margin={{ top: 'm' }}>
              <Header variant="h3">Operator bindings ({aliases.length})</Header>
              <Table
                items={aliases}
                variant="embedded"
                trackBy="price_list_name"
                columnDefinitions={[
                  { id: 'name', header: 'Price List name', cell: (a) => <Box variant="samp">{a.price_list_name}</Box> },
                  { id: 'model', header: 'Bound model id', cell: (a) => <Box variant="samp">{a.model_id}</Box> },
                  { id: 'when', header: 'Updated', cell: (a) => (a.updated_at ? ago(a.updated_at) : '–'), width: 140 },
                  {
                    id: 'rm',
                    header: '',
                    cell: (a) => <Button variant="inline-link" onClick={() => void unbind(a.price_list_name)}>Remove</Button>,
                    width: 110,
                  },
                ]}
              />
            </Box>
          )}
        </ExpandableSection>
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
      <BindModal
        row={binding}
        onDismiss={() => setBinding(null)}
        onSaved={(name) => {
          setFlash({ type: 'success', content: `"${name}" bound. Run "Refresh from AWS" to price it now.` });
          setBinding(null);
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
      // pre-fill with the current effective per-1M rate (the API's native unit)
      setInp(row.effective.input_per_1m != null ? String(row.effective.input_per_1m) : '');
      setOut(row.effective.output_per_1m != null ? String(row.effective.output_per_1m) : '');
      setNote(row.override?.note ?? '');
      setErr(null);
    }
  }, [row]);

  if (!row) return null;

  const save = async () => {
    setErr(null);
    const body: Record<string, unknown> = {};
    const parse = (s: string): number | null => (s.trim() === '' ? null : parseFloat(s));
    const pin = parse(inp);
    const pout = parse(out);
    if ((pin !== null && (Number.isNaN(pin) || pin < 0)) || (pout !== null && (Number.isNaN(pout) || pout < 0))) {
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
            Rates are USD per <b>1M tokens</b> (how AWS quotes them). The override wins over the
            AWS-published rate for {row.model}, applies to every tier and routing mode, and is audited.
            {row.effective.source === 'aws-published' && (
              <> Current AWS-published (in-region standard): input {perM(row.effective.input_per_1m)}, output {perM(row.effective.output_per_1m)}.</>
            )}
          </Box>
          <FormField label="Input rate (USD per 1M tokens)">
            <Input value={inp} onChange={({ detail }) => setInp(detail.value)} type="number" inputMode="decimal" placeholder="e.g. 3.00" />
          </FormField>
          <FormField label="Output rate (USD per 1M tokens)">
            <Input value={out} onChange={({ detail }) => setOut(detail.value)} type="number" inputMode="decimal" placeholder="e.g. 15.00" />
          </FormField>
          <FormField label="Note — optional" description="Why this custom rate (shown in the catalog + audit trail).">
            <Input value={note} onChange={({ detail }) => setNote(detail.value)} placeholder="e.g. negotiated EDP rate" />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

function BindModal(props: { row: UnmatchedRow | null; onDismiss: () => void; onSaved: (name: string) => void }) {
  const { row, onDismiss, onSaved } = props;
  const [modelId, setModelId] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useMemo(() => {
    if (row) {
      setModelId('');
      setErr(null);
    }
  }, [row]);

  if (!row) return null;

  const save = async () => {
    setErr(null);
    const id = modelId.trim();
    if (!/^[a-z0-9]+\.[a-z0-9][a-z0-9.:\-]*$/.test(id)) {
      setErr('Enter a Bedrock model id like vendor.model-name (lowercase).');
      return;
    }
    setSaving(true);
    try {
      await api.post('/pricing/alias', { price_list_name: row.price_list_name, model_id: id });
      onSaved(row.price_list_name);
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
      header={`Bind "${row.price_list_name}" to a model id`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={saving}>Cancel</Button>
            <Button variant="primary" onClick={() => void save()} loading={saving}>Bind</Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {err && <Alert type="error">{err}</Alert>}
        <Box variant="p" color="text-body-secondary">
          The AWS Price List publishes a token rate under this name, but it could not be bound to a
          Bedrock model id without guessing. Enter the model id it belongs to (as the gateway invokes
          it); the binding is audited, outranks automatic matching, and applies on the next refresh.
        </Box>
        <FormField label="Bedrock model id" description='e.g. "mistral.ministral-3-8b-instruct"'>
          <Input value={modelId} onChange={({ detail }) => setModelId(detail.value)} placeholder="vendor.model-name" />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
}
