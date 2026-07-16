// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Same-origin API client. The access token (whose cognito:groups claim the
// Lambda authorizes against) travels as a Bearer header; 401 surfaces as a
// session-expired signal the shell turns into a re-login.

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let tokenProvider: () => string | undefined = () => undefined;

export function setTokenProvider(fn: () => string | undefined): void {
  tokenProvider = fn;
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = tokenProvider();
  const res = await fetch(`/api${path}`, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text.slice(0, 200) };
  }
  if (!res.ok) {
    const msg =
      (data as { error?: string; message?: string }).error ??
      (data as { message?: string }).message ??
      `HTTP ${res.status}`;
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => call<T>('GET', path),
  put: <T>(path: string, body: unknown) => call<T>('PUT', path, body),
  post: <T>(path: string, body: unknown) => call<T>('POST', path, body),
  del: <T>(path: string) => call<T>('DELETE', path),
};
