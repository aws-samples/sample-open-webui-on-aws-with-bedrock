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
  ColumnLayout,
  Container,
  ContentLayout,
  ExpandableSection,
  Flashbar,
  Header,
  Modal,
  Form,
  FormField,
  Input,
  Pagination,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { useMemo, useState } from 'react';
import { api, ApiError } from '../api';
import { useApiData } from '../components/useApiData';
import { ago } from '../format';
import type {
  CoverageModel,
  EffectiveGrid,
  ModuleConfig,
  PriceRow,
  PricingCatalog,
  RateGrid,
  UnmatchedRow,
} from '../types';

const PAGE = 25;

/** Rates arrive as USD per 1M tokens (how AWS quotes). */
function perM(v: number | null | undefined): string {
  if (v === undefined || v === null) return '–';
  return `$${v.toLocaleString('en-US', { maximumFractionDigits: 2 })}/M`;
}

/**
 * Read the server-computed effective grid (D11): routing → direction → per-1M.
 * Replaces the deleted local gridStd() resolver re-implementation — the server
 * now runs the production resolver and hands us the standard/default chain
 * result, so the console renders truth instead of re-deriving it.
 */
function effGrid(grid: EffectiveGrid | null | undefined, routing: string, direction: string): number | null {
  const v = grid?.[routing]?.[direction];
  return typeof v === 'number' ? v : null;
}

/** Unmatched candidate rates are the raw 4-level grid; read the standard leaf. */
function candStd(rates: RateGrid | null | undefined, routing: string, direction: string): number | null {
  const v = rates?.[routing]?.standard?.default?.[direction];
  return typeof v === 'number' ? v : null;
}

/** Shared column set for the ambiguous + historical unmatched tables. */
function unmatchedColumns(onBind: (u: UnmatchedRow) => void) {
  return [
    { id: 'name', header: 'Price List name', cell: (u: UnmatchedRow) => <Box variant="samp">{u.price_list_name}</Box>, minWidth: 220 },
    { id: 'provider', header: 'Provider', cell: (u: UnmatchedRow) => u.provider || '–', width: 120 },
    { id: 'svc', header: 'Offer file', cell: (u: UnmatchedRow) => <Box fontSize="body-s">{u.service_code || '–'}</Box>, width: 220 },
    {
      id: 'reason',
      header: 'Reason',
      cell: (u: UnmatchedRow) => {
        const ambiguous = u.class === 'ambiguous' || u.reason === 'ambiguous-match';
        return (
          <StatusIndicator type={ambiguous ? 'warning' : 'info'}>
            {u.class ?? u.reason}
          </StatusIndicator>
        );
      },
      width: 160,
    },
    {
      id: 'rates',
      header: 'Published in / out',
      cell: (u: UnmatchedRow) => (
        <Box textAlign="right">
          {perM(candStd(u.candidate_rates, 'in_region', 'input') ?? candStd(u.candidate_rates, 'global', 'input'))}
          {' / '}
          {perM(candStd(u.candidate_rates, 'in_region', 'output') ?? candStd(u.candidate_rates, 'global', 'output'))}
        </Box>
      ),
      width: 160,
    },
    {
      id: 'bind',
      header: '',
      cell: (u: UnmatchedRow) => <Button variant="inline-link" onClick={() => onBind(u)}>Bind to model id</Button>,
      width: 150,
    },
  ];
}

function SourceBadge({ source }: { source: PriceRow['effective']['source'] }) {
  if (source === 'override') return <StatusIndicator type="info">override</StatusIndicator>;
  if (source === 'aws-published') return <StatusIndicator type="success">AWS published</StatusIndicator>;
  return <StatusIndicator type="warning">unpriced</StatusIndicator>;
}

/** Per-row gateway coverage badges (D11): invokable / listed lanes / stale caps. */
function GatewayBadges({ gw }: { gw: PriceRow['gateway'] }) {
  if (!gw) return <Box color="text-body-secondary">–</Box>;
  const badges = [];
  if (gw.available) badges.push(<Badge key="inv" color="green">invokable</Badge>);
  else if (gw.listed) badges.push(<Badge key="stale" color="red">stale caps</Badge>);
  else badges.push(<Badge key="na" color="grey">not available</Badge>);
  if (gw.listed && gw.lanes.length) {
    badges.push(
      <Badge key="lanes" color="blue">{gw.lanes.map((l) => l.replace('_', '-')).join(' · ')}</Badge>,
    );
  } else if (gw.available && !gw.listed) {
    badges.push(<Badge key="unlisted" color="grey">unlisted</Badge>);
  }
  return <SpaceBetween direction="horizontal" size="xxs">{badges}</SpaceBetween>;
}

const COVERAGE_REASON: Record<string, string> = {
  'no-pricing-row': 'No AWS-published rate and no operator override — an AWS publishing gap.',
  'null-rates': 'A pricing row exists but resolves to null input/output rates.',
  'stale-caps': 'Listed in served capabilities but no longer available on the live gateway catalog.',
  ok: 'Priced.',
};

/**
 * Coverage summary strip (D11): invokable & priced / invokable & UNPRICED /
 * listed-but-unavailable, with the unpriced set called out prominently by name
 * and reason. Renders a graceful notice when coverage is absent (§5 404/null).
 */
function CoverageStrip({ meta }: { meta: PricingCatalog['meta'] | undefined }) {
  const cov = meta?.coverage;
  if (!cov) {
    return (
      <Container header={<Header variant="h2">Gateway coverage</Header>}>
        <Alert type="info">
          No coverage snapshot yet. Coverage joins the served model capabilities and the live gateway
          catalog to the price catalog; it is computed at the end of each pricing refresh. Click
          “Refresh from AWS” to compute it.
        </Alert>
      </Container>
    );
  }
  const models = cov.models ?? [];
  const unpriced = models.filter((m) => m.catalog_available && !m.priced);
  const listedNa = models.filter((m) => m.listed && !m.catalog_available);
  return (
    <Container
      header={
        <Header
          variant="h2"
          description={cov.computed_at ? `Joined ${ago(cov.computed_at)}.` : undefined}
        >
          Gateway coverage
        </Header>
      }
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Invokable &amp; priced</Box>
            <Box fontSize="display-l" fontWeight="bold" color="text-status-success">
              {cov.invokable_priced}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">of {cov.invokable} invokable</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Invokable &amp; UNPRICED</Box>
            <Box
              fontSize="display-l"
              fontWeight="bold"
              color={cov.invokable_unpriced > 0 ? 'text-status-error' : 'text-status-success'}
            >
              {cov.invokable_unpriced}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              {cov.invokable_unpriced > 0 ? 'alarmed — priced from no source' : 'none'}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Listed but unavailable</Box>
            <Box fontSize="display-l" fontWeight="bold" color="text-status-info">
              {cov.listed_not_available}
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">stale caps — not a quota risk</Box>
          </div>
        </ColumnLayout>

        {unpriced.length > 0 && (
          <Alert type="warning" header={`${unpriced.length} invokable model${unpriced.length > 1 ? 's are' : ' is'} UNPRICED`}>
            <Table
              variant="embedded"
              items={unpriced}
              trackBy="id"
              columnDefinitions={[
                { id: 'id', header: 'Model', cell: (m: CoverageModel) => <Box variant="samp">{m.id}</Box>, minWidth: 260 },
                {
                  id: 'lanes',
                  header: 'Lanes',
                  cell: (m) => (m.lanes.length ? m.lanes.map((l) => l.replace('_', '-')).join(' · ') : <Box color="text-body-secondary">unlisted</Box>),
                  width: 200,
                },
                {
                  id: 'reason',
                  header: 'Reason',
                  cell: (m) => (
                    <Popover dismissButton={false} position="top" size="medium" triggerType="text" content={COVERAGE_REASON[m.reason] ?? m.reason}>
                      <StatusIndicator type="warning">{m.reason}</StatusIndicator>
                    </Popover>
                  ),
                  minWidth: 180,
                },
              ]}
            />
          </Alert>
        )}

        {listedNa.length > 0 && (
          <ExpandableSection
            headerText={`Listed but unavailable (${listedNa.length})`}
            variant="footer"
            headerDescription="In served capabilities but not on the live gateway catalog — stale caps, visible but not a quota risk."
          >
            <SpaceBetween direction="horizontal" size="xxs">
              {listedNa.map((m) => (
                <Badge key={m.id} color="grey">{m.id}</Badge>
              ))}
            </SpaceBetween>
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Container>
  );
}

export default function PricingPage() {
  const { data, loading, error, refresh } = useApiData<PricingCatalog>('/pricing');
  const { data: cfg } = useApiData<ModuleConfig>('/config');
  const [filterText, setFilterText] = useState('');
  const [showUnpriced, setShowUnpriced] = useState(false);
  const [showHistorical, setShowHistorical] = useState(false);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<PriceRow[]>([]);
  const [editing, setEditing] = useState<PriceRow | null>(null);
  const [binding, setBinding] = useState<UnmatchedRow | null>(null);
  const [flash, setFlash] = useState<{ type: 'success' | 'error'; content: string } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const rows = data?.models ?? [];
  const unmatched = data?.unmatched ?? [];
  const aliases = data?.aliases ?? [];
  const modelIdPattern = cfg?.pricing?.model_id_pattern;

  // D8: ambiguous entries are actionable and stay prominent; no-match entries are
  // historical (legacy display-name products with no control-plane twin) and are
  // collapsed by default behind a "show historical" toggle.
  const isHistorical = (u: UnmatchedRow) =>
    u.class === 'no-match' || (u.class === undefined && u.reason === 'no-control-plane-match');
  const ambiguousUnmatched = useMemo(() => unmatched.filter((u) => !isHistorical(u)), [unmatched]);
  const historicalUnmatched = useMemo(() => unmatched.filter(isHistorical), [unmatched]);
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

        <CoverageStrip meta={data?.meta} />

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
                    : `${perM(effGrid(m.effective_grid, 'in_region', 'input'))} / ${perM(effGrid(m.effective_grid, 'in_region', 'output'))}`}
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
                        {perM(effGrid(m.effective_grid, 'global', 'input'))} / {perM(effGrid(m.effective_grid, 'global', 'output'))}
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
            { id: 'gateway', header: 'Gateway', cell: (m) => <GatewayBadges gw={m.gateway} />, minWidth: 170 },
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

        <Container
          header={
            <Header
              variant="h2"
              counter={`(${ambiguousUnmatched.length} actionable · ${historicalUnmatched.length} historical)`}
              description="AWS publishes a token rate for these, but no Bedrock model id could be resolved without guessing. Bind a model id to price it on the next refresh — bindings outrank automatic matching."
              actions={
                historicalUnmatched.length > 0 ? (
                  <Button
                    variant={showHistorical ? 'primary' : 'normal'}
                    iconName={showHistorical ? 'angle-up' : 'angle-down'}
                    onClick={() => setShowHistorical((v) => !v)}
                  >
                    {showHistorical ? 'Hide historical' : `Show historical (${historicalUnmatched.length})`}
                  </Button>
                ) : undefined
              }
            >
              Unmatched Price List entries
            </Header>
          }
        >
          <SpaceBetween size="l">
            {/* D8: ambiguous = actionable (refresher refused to guess between >1 twins), alarmed. */}
            <div>
              <Header variant="h3" description="Multiple candidate model ids — the refresher refused to guess. Bind the correct one.">
                Ambiguous ({ambiguousUnmatched.length})
              </Header>
              <Table
                items={ambiguousUnmatched}
                variant="embedded"
                trackBy="price_list_name"
                columnDefinitions={unmatchedColumns((u) => setBinding(u))}
                empty={<Box textAlign="center" padding="m" color="inherit">No ambiguous entries — nothing needs an operator decision.</Box>}
              />
            </div>

            {/* D8: no-match = historical (legacy display-name products, no live twin), collapsed. */}
            {showHistorical && historicalUnmatched.length > 0 && (
              <div>
                <Header variant="h3" description="Legacy Price List products with no current control-plane twin. Kept and counted, not alarmed.">
                  Historical / no-match ({historicalUnmatched.length})
                </Header>
                <Table
                  items={historicalUnmatched}
                  variant="embedded"
                  trackBy="price_list_name"
                  columnDefinitions={unmatchedColumns((u) => setBinding(u))}
                />
              </div>
            )}

            {aliases.length > 0 && (
              <div>
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
              </div>
            )}
          </SpaceBetween>
        </Container>
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
        modelIdPattern={modelIdPattern}
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
          <FormField
            label="Provenance note — optional"
            description="Where this rate came from (shown in the catalog + audit trail). For a publishing-gap override, cite the AWS model-card page / tier."
          >
            <Input value={note} onChange={({ detail }) => setNote(detail.value)} placeholder="e.g. AWS model-card 272K-ctx standard tier, 2026-08" />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

function BindModal(props: {
  row: UnmatchedRow | null;
  modelIdPattern?: string;
  onDismiss: () => void;
  onSaved: (name: string) => void;
}) {
  const { row, modelIdPattern, onDismiss, onSaved } = props;
  const [modelId, setModelId] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // D11: validate against the server-supplied identity.MODEL_ID_RE source
  // (meta.model_id_pattern) rather than a console-local literal — one source of
  // truth for what a settle-reachable model id is. Compile once per pattern.
  const idRe = useMemo(() => {
    if (!modelIdPattern) return null;
    try {
      return new RegExp(modelIdPattern);
    } catch {
      return null;
    }
  }, [modelIdPattern]);

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
    // Client-side gate uses the server pattern when present; when the pattern
    // could not be loaded the server still validates on PUT (defense in depth).
    if (idRe && !idRe.test(id)) {
      setErr('Enter a Bedrock model id like vendor.model-name (lowercase).');
      return;
    }
    if (!idRe && !id) {
      setErr('Enter a Bedrock model id.');
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
