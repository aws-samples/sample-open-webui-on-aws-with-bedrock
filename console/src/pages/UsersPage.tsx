// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Users: spend-ordered counters for a window (by-window GSI), Cognito email
// search for users with no usage yet, near/over-limit filters, and the three
// admin actions (limits, override, reset) on the selected row.

import {
  Box,
  Button,
  ContentLayout,
  Flashbar,
  FormField,
  Header,
  Input,
  Link,
  Pagination,
  SegmentedControl,
  Select,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';
import { useContext, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import { SelfContext, useModule } from '../App';
import { PolicyModal, type PolicyTarget } from '../components/PolicyModal';
import { QuotaBar, QuotaStatus } from '../components/QuotaBar';
import { ResetCounterModal } from '../components/ResetCounterModal';
import { recentWindows, tokens, usd } from '../format';
import type { Policy, SearchUser, UserRow } from '../types';

type Filter = 'all' | 'near-limit' | 'over-limit';
const PAGE = 25;

export default function UsersPage() {
  const module = useModule();
  const self = useContext(SelfContext);
  const navigate = useNavigate();
  const [windowSel, setWindowSel] = useState(module.window);
  const [filter, setFilter] = useState<Filter>('all');
  const [rows, setRows] = useState<UserRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<UserRow[]>([]);
  const [q, setQ] = useState('');
  const [searchResults, setSearchResults] = useState<SearchUser[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [policyTarget, setPolicyTarget] = useState<PolicyTarget | null>(null);
  const [resetTarget, setResetTarget] = useState<{ sub: string; label: string; window: string; totalUsd: number } | null>(null);
  const [flash, setFlash] = useState<{ type: 'success' | 'error'; content: string } | null>(null);

  const load = (reset = true) => {
    setLoading(true);
    const cur = reset ? '' : cursor ? `&cursor=${encodeURIComponent(cursor)}` : '';
    api
      .get<{ users: UserRow[]; cursor: string | null }>(
        `/users?window=${windowSel}&limit=100${cur}${filter !== 'all' ? `&filter=${filter}` : ''}`,
      )
      .then((d) => {
        setRows((prev) => (reset ? d.users : [...prev, ...d.users]));
        setCursor(d.cursor);
        if (reset) setPage(1);
      })
      .catch((e: Error) => setFlash({ type: 'error', content: `Failed to load users: ${e.message}` }))
      .finally(() => setLoading(false));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => load(true), [windowSel, filter]);

  // Cognito search (email prefix or exact sub) — covers users with no counter yet.
  useEffect(() => {
    const trimmed = q.trim();
    if (trimmed.length < 2) {
      setSearchResults(null);
      return;
    }
    const t = setTimeout(() => {
      setSearching(true);
      api
        .get<{ users: SearchUser[] }>(`/users/search?q=${encodeURIComponent(trimmed)}`)
        .then((d) => setSearchResults(d.users))
        .catch(() => setSearchResults([]))
        .finally(() => setSearching(false));
    }, 350);
    return () => clearTimeout(t);
  }, [q]);

  const visible = useMemo(() => rows.slice((page - 1) * PAGE, page * PAGE), [rows, page]);
  const sel = selected[0];

  const openPolicy = async (u: { sub: string; label: string }, timebox: boolean) => {
    let existing: Partial<Policy> | undefined;
    try {
      existing = await api.get<Partial<Policy>>(`/policy/${encodeURIComponent(`USER#${u.sub}`)}`);
      if (existing && Object.keys(existing).length === 0) existing = undefined;
    } catch {
      existing = undefined;
    }
    setPolicyTarget({ scope: `USER#${u.sub}`, label: u.label, existing, timeboxDefault: timebox });
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Per-user consumption against quota for a monthly window. Search finds any signed-up user, including those with no usage yet."
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                disabled={!sel}
                onClick={() => sel && void openPolicy({ sub: sel.sub, label: sel.email || sel.sub }, false)}
              >
                Set limits
              </Button>
              <Button
                disabled={!sel}
                onClick={() => sel && void openPolicy({ sub: sel.sub, label: sel.email || sel.sub }, true)}
              >
                Grant override
              </Button>
              <Button
                disabled={!sel || sel.sub === self}
                onClick={() =>
                  sel && setResetTarget({ sub: sel.sub, label: sel.email || sel.sub, window: windowSel, totalUsd: sel.total_usd })
                }
              >
                Reset counter
              </Button>
            </SpaceBetween>
          }
        >
          Users
        </Header>
      }
    >
      <SpaceBetween size="m">
        {flash && (
          <Flashbar
            items={[{ ...flash, dismissible: true, onDismiss: () => setFlash(null), id: 'users-flash' }]}
          />
        )}

        <SpaceBetween direction="horizontal" size="m">
          <FormField label="Search directory">
            <Input
              value={q}
              onChange={({ detail }) => setQ(detail.value)}
              placeholder="email prefix or exact sub…"
              type="search"
              clearAriaLabel="Clear search"
            />
          </FormField>
          <FormField label="Window">
            <Select
              selectedOption={{ label: windowSel, value: windowSel }}
              onChange={({ detail }) => setWindowSel(detail.selectedOption.value!)}
              options={recentWindows().map((w) => ({ label: w, value: w }))}
            />
          </FormField>
          <FormField label="Show">
            <SegmentedControl
              selectedId={filter}
              onChange={({ detail }) => setFilter(detail.selectedId as Filter)}
              options={[
                { id: 'all', text: 'All' },
                { id: 'near-limit', text: '≥80%' },
                { id: 'over-limit', text: 'Over limit' },
              ]}
            />
          </FormField>
        </SpaceBetween>

        {searchResults !== null ? (
          <Table
            header={<Header counter={`(${searchResults.length})`}>Directory results</Header>}
            items={searchResults}
            loading={searching}
            loadingText="Searching the user pool"
            trackBy="sub"
            columnDefinitions={[
              {
                id: 'email',
                header: 'Email',
                cell: (u: SearchUser) => (
                  <Link onFollow={(e) => { e.preventDefault(); navigate(`/users/${u.sub}`); }} href={`/users/${u.sub}`}>
                    {u.email || u.sub}
                  </Link>
                ),
              },
              { id: 'name', header: 'Name', cell: (u: SearchUser) => u.name || '–' },
              { id: 'groups', header: 'Groups', cell: (u: SearchUser) => u.groups.join(', ') || '–' },
              { id: 'status', header: 'Status', cell: (u: SearchUser) => u.status },
              {
                id: 'act',
                header: 'Actions',
                cell: (u: SearchUser) => (
                  <Button variant="inline-link" onClick={() => void openPolicy({ sub: u.sub, label: u.email || u.sub }, false)}>
                    Set limits
                  </Button>
                ),
              },
            ]}
            empty={<Box textAlign="center" padding="l">No pool users match “{q}”</Box>}
          />
        ) : (
          <Table
            header={
              <Header counter={rows.length ? `(${rows.length}${cursor ? '+' : ''})` : undefined}>
                Consumption — {windowSel}
              </Header>
            }
            items={visible}
            loading={loading}
            loadingText="Loading counters"
            selectionType="single"
            selectedItems={selected}
            onSelectionChange={({ detail }) => setSelected(detail.selectedItems as UserRow[])}
            trackBy="sub"
            pagination={
              <Pagination
                currentPageIndex={page}
                pagesCount={Math.max(1, Math.ceil(rows.length / PAGE))}
                onChange={({ detail }) => setPage(detail.currentPageIndex)}
                openEnd={!!cursor}
                onNextPageClick={({ detail }) => {
                  if (detail.requestedPageIndex > Math.ceil(rows.length / PAGE) && cursor) load(false);
                }}
              />
            }
            columnDefinitions={[
              {
                id: 'email',
                header: 'User',
                cell: (u: UserRow) => (
                  <Link onFollow={(e) => { e.preventDefault(); navigate(`/users/${u.sub}`); }} href={`/users/${u.sub}`}>
                    {u.email || u.sub}
                  </Link>
                ),
                minWidth: 220,
              },
              { id: 'spend', header: 'Spend', cell: (u) => <Box textAlign="right">{usd(u.total_usd)}</Box>, width: 110 },
              {
                id: 'quota',
                header: 'Quota consumption',
                cell: (u) => <QuotaBar totalUsd={u.total_usd} hardUsd={u.hard_limit_usd} pct={u.pct_of_limit} />,
                minWidth: 180,
              },
              { id: 'status', header: 'Status', cell: (u) => <QuotaStatus pct={u.pct_of_limit} />, width: 140 },
              {
                id: 'tokens',
                header: 'Tokens in / out',
                cell: (u) => `${tokens(u.used_in)} / ${tokens(u.used_out)}`,
                width: 150,
              },
              { id: 'calls', header: 'Calls', cell: (u) => <Box textAlign="right">{u.req_count ?? 0}</Box>, width: 90 },
            ]}
            empty={
              <Box textAlign="center" padding="xl">
                <b>{filter === 'all' ? 'No usage recorded this window' : 'Nobody matches this filter'}</b>
                <Box variant="p" color="inherit">
                  {filter === 'all'
                    ? 'Counters appear as soon as users start chatting. Use the directory search above to set limits ahead of usage.'
                    : 'No users are at this consumption level — good news.'}
                </Box>
              </Box>
            }
          />
        )}
      </SpaceBetween>

      <PolicyModal
        target={policyTarget}
        onDismiss={() => setPolicyTarget(null)}
        onSaved={() => {
          setFlash({ type: 'success', content: 'Policy saved. New limits apply to the next request (≤60 s cache).' });
          load(true);
        }}
      />
      <ResetCounterModal
        target={resetTarget}
        onDismiss={() => setResetTarget(null)}
        onDone={() => {
          setFlash({ type: 'success', content: 'Counter reset. The user can chat again immediately.' });
          load(true);
        }}
      />
    </ContentLayout>
  );
}
