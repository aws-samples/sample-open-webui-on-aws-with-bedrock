// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Create/edit a quota policy or grant a time-boxed override. One modal serves
// Users (USER# scope), Groups (GROUP# scope), and Policies (any scope).

import {
  Alert,
  Box,
  Button,
  DatePicker,
  Form,
  FormField,
  Input,
  Modal,
  Select,
  SpaceBetween,
} from '@cloudscape-design/components';
import { useContext, useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import { SelfContext } from '../App';
import type { Policy } from '../types';

export interface PolicyTarget {
  scope: string; // DEFAULT | GROUP#<name> | USER#<sub>
  label: string; // human name shown in the header
  existing?: Partial<Policy>;
  timeboxDefault?: boolean; // pre-select the override expiry UI
}

export function PolicyModal(props: {
  target: PolicyTarget | null;
  onDismiss: () => void;
  onSaved: (p: Partial<Policy>) => void;
}) {
  const { target, onDismiss, onSaved } = props;
  const self = useContext(SelfContext);
  const [hard, setHard] = useState('');
  const [soft, setSoft] = useState('');
  const [rpm, setRpm] = useState('');
  const [note, setNote] = useState('');
  const [expiry, setExpiry] = useState<{ label: string; value: string } | null>(null);
  const [customDate, setCustomDate] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!target) return;
    setHard(target.existing?.hard_limit_usd?.toString() ?? '');
    setSoft(target.existing?.soft_limit_usd?.toString() ?? '');
    setRpm(target.existing?.rpm_limit?.toString() ?? '');
    setNote(target.existing?.note ?? '');
    setExpiry(target.timeboxDefault ? EXPIRY_OPTIONS[1] : EXPIRY_OPTIONS[0]);
    setCustomDate('');
    setErr(null);
  }, [target]);

  if (!target) return null;
  const isSelf = target.scope === `USER#${self}`;

  const save = async () => {
    setErr(null);
    const hardN = parseFloat(hard);
    if (Number.isNaN(hardN) || hardN < 0) {
      setErr('Hard limit must be a non-negative number (0 = block all).');
      return;
    }
    const body: Record<string, unknown> = { hard_limit_usd: hardN };
    if (soft.trim() !== '') {
      const softN = parseFloat(soft);
      if (Number.isNaN(softN) || softN < 0 || softN > hardN) {
        setErr('Soft (warn) limit must be between 0 and the hard limit.');
        return;
      }
      body.soft_limit_usd = softN;
    }
    if (rpm.trim() !== '') {
      const rpmN = parseInt(rpm, 10);
      if (Number.isNaN(rpmN) || rpmN < 0) {
        setErr('Requests/minute must be a non-negative integer.');
        return;
      }
      body.rpm_limit = rpmN;
    }
    if (note.trim()) body.note = note.trim();
    const until = resolveExpiry(expiry?.value ?? 'none', customDate);
    if (until === 'invalid') {
      setErr('Pick a future expiry date.');
      return;
    }
    if (until) body.until = until;

    setSaving(true);
    try {
      const saved = await api.put<Partial<Policy>>(
        `/policy/${encodeURIComponent(target.scope)}`,
        body,
      );
      onSaved(saved);
      onDismiss();
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
      header={target.existing ? `Edit limits — ${target.label}` : `Set limits — ${target.label}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void save()} loading={saving} disabled={isSelf}>
              Save policy
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form>
        <SpaceBetween size="m">
          {isSelf && (
            <Alert type="warning" header="Self-service changes are not allowed">
              Administrators cannot change their own limits — ask another administrator (four-eyes
              rule; the API enforces this too).
            </Alert>
          )}
          {err && <Alert type="error">{err}</Alert>}
          <FormField
            label="Hard limit (USD per month)"
            description="Requests are blocked once total spend reaches this amount."
          >
            <Input value={hard} onChange={({ detail }) => setHard(detail.value)} type="number" inputMode="decimal" />
          </FormField>
          <FormField
            label="Soft limit (USD) — optional"
            description="Users see an in-chat warning when they cross this amount."
          >
            <Input value={soft} onChange={({ detail }) => setSoft(detail.value)} type="number" inputMode="decimal" />
          </FormField>
          <FormField label="Requests per minute — optional" description="Per-user rate limit (default 30).">
            <Input value={rpm} onChange={({ detail }) => setRpm(detail.value)} type="number" inputMode="numeric" />
          </FormField>
          <FormField
            label="Expires"
            description="Records an expiry date on the policy so operators can track and clean it up. NOTE: the row is not auto-removed — the Quota policies page flags it red once past its date; delete it there to end the override. Permanent unless set."
          >
            <Select
              selectedOption={expiry}
              onChange={({ detail }) => setExpiry(detail.selectedOption as { label: string; value: string })}
              options={EXPIRY_OPTIONS}
            />
          </FormField>
          {expiry?.value === 'custom' && (
            <FormField label="Expiry date">
              <DatePicker value={customDate} onChange={({ detail }) => setCustomDate(detail.value)} placeholder="YYYY/MM/DD" />
            </FormField>
          )}
          <FormField label="Note — optional" description="Why this limit exists (shown in the policy list and audit trail).">
            <Input value={note} onChange={({ detail }) => setNote(detail.value)} placeholder="e.g. Q3 analytics project bump — ticket 1234" />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}

const EXPIRY_OPTIONS = [
  { label: 'Never (permanent policy)', value: 'none' },
  { label: 'In 7 days', value: '7d' },
  { label: 'In 30 days', value: '30d' },
  { label: 'End of this month', value: 'eom' },
  { label: 'Custom date…', value: 'custom' },
];

function resolveExpiry(kind: string, customDate: string): number | null | 'invalid' {
  const now = Date.now();
  switch (kind) {
    case '7d':
      return Math.floor(now / 1000) + 7 * 86400;
    case '30d':
      return Math.floor(now / 1000) + 30 * 86400;
    case 'eom': {
      const d = new Date();
      return Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1) / 1000);
    }
    case 'custom': {
      if (!customDate) return 'invalid';
      const ts = Math.floor(new Date(`${customDate}T23:59:59Z`).getTime() / 1000);
      return Number.isNaN(ts) || ts * 1000 <= now ? 'invalid' : ts;
    }
    default:
      return null;
  }
}
