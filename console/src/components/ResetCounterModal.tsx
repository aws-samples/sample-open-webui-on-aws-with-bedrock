// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { useContext, useState } from 'react';
import { api, ApiError } from '../api';
import { SelfContext } from '../App';
import { usd } from '../format';

export function ResetCounterModal(props: {
  target: { sub: string; label: string; window: string; totalUsd: number } | null;
  onDismiss: () => void;
  onDone: () => void;
}) {
  const { target, onDismiss, onDone } = props;
  const self = useContext(SelfContext);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!target) return null;
  const isSelf = target.sub === self;

  const reset = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.post('/counter-reset', { sub: target.sub, window: target.window });
      onDone();
      onDismiss();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={`Reset usage counter — ${target.label}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void reset()} loading={busy} disabled={isSelf}>
              Reset counter
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {isSelf && (
          <Alert type="warning" header="Self-service resets are not allowed">
            Administrators cannot reset their own counters — ask another administrator.
          </Alert>
        )}
        {err && <Alert type="error">{err}</Alert>}
        <Box variant="p">
          This zeroes the <b>{target.window}</b> counter (currently {usd(target.totalUsd)}), restoring
          access immediately if the user was blocked. The action is recorded in the audit trail; the
          usage ledger keeps the historical calls.
        </Box>
      </SpaceBetween>
    </Modal>
  );
}
