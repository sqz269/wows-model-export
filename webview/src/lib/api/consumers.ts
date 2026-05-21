// `/api/consumers/*` client.
//
// Lists registered downstream consumer plugins and routes action runs
// through the same /api/extract/jobs polling path the extract route
// already uses — jobs of kind="consumer" surface through GET
// /api/extract/jobs/{id} unchanged.

import { fetchJson } from './client';
import type { Consumer, ConsumersResponse } from '$lib/types/consumers';
import type { RunResponse } from './extract';

/** Fetch the list of registered consumers. Discovery is cached on the
 *  server for the process lifetime; this client does NOT cache because
 *  list shape is small and the cost of an extra round-trip is trivial. */
export async function fetchConsumers(): Promise<Consumer[]> {
  const body = await fetchJson<ConsumersResponse>('/api/consumers');
  return body.consumers ?? [];
}

/** Kick off a consumer action. Returns the spawn-job envelope; the
 *  caller poll-loops `/api/extract/jobs/{id}` exactly like an extract
 *  or skin-pack run. */
export async function runConsumerAction(
  consumerId: string,
  actionId: string,
  body: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; body: RunResponse }> {
  const res = await fetch(
    `/api/consumers/${encodeURIComponent(consumerId)}/${encodeURIComponent(actionId)}/run`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  );
  let parsed: RunResponse;
  try {
    parsed = (await res.json()) as RunResponse;
  } catch {
    parsed = { ok: false } as RunResponse;
  }
  return { ok: res.ok, status: res.status, body: parsed };
}
