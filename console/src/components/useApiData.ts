// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';

/** Fetch-on-mount + manual refresh with loading/error state. */
export function useApiData<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(() => {
    if (!path) return;
    const mySeq = ++seq.current;
    setLoading(true);
    setError(null);
    api
      .get<T>(path)
      .then((d) => {
        if (seq.current === mySeq) setData(d);
      })
      .catch((e: ApiError) => {
        if (seq.current === mySeq) setError(e);
      })
      .finally(() => {
        if (seq.current === mySeq) setLoading(false);
      });
  }, [path]);

  useEffect(refresh, [refresh]);
  return { data, loading, error, refresh };
}
