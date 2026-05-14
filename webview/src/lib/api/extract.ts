// Extract page API client.
//
// The snapshot endpoint pays a ~30 s GameParams parse on cold cache, so
// the result is module-cached for the lifetime of the SPA — subsequent
// page visits hit the in-memory copy. Job-state polling does NOT cache;
// each tick must reach the dev server to pick up new stdout/stderr.

import { fetchJson } from './client';
import type {
  ExtractedShip,
  GpStatus,
  JobState,
  SnapshotResponse,
} from '$lib/types/extract';

let _snapshotCache: Promise<SnapshotResponse> | null = null;

/** Fetch the full Vehicles + permoflages snapshot. Cached for the
 *  SPA lifetime; call `invalidateSnapshot()` after a `wows-snapshot`
 *  refresh / GameParams patch to drop the cache. */
export function fetchExtractSnapshot(): Promise<SnapshotResponse> {
  if (!_snapshotCache) {
    _snapshotCache = fetchJson<SnapshotResponse>('/api/extract/snapshot').catch((err) => {
      _snapshotCache = null;
      throw err;
    });
  }
  return _snapshotCache;
}

export function invalidateSnapshot(): void {
  _snapshotCache = null;
}

export function fetchGameparamsStatus(): Promise<GpStatus> {
  return fetchJson<GpStatus>('/api/gameparams/status');
}

interface ExtractedShipsResponse {
  ships?: ExtractedShip[];
}

/** Already-extracted ship list (drives the skin-pack target dropdown).
 *  Re-fetched on every call — the user may have ingested a new ship and
 *  expects it to appear without a page reload. */
export async function fetchExtractedShips(): Promise<ExtractedShip[]> {
  const body = await fetchJson<ExtractedShipsResponse>('/api/ships');
  return body.ships ?? [];
}

export interface RunExtractBody {
  vehicle: string;
  label: string;
  permoflage: string | null;
  skip_legacy: boolean;
  build_library: boolean;
  and_publish: boolean;
  publish_force: boolean;
}

export interface RunSkinBody {
  ship: string;
  source: 'wg' | 'vfs' | 'loose';
  source_arg: string;
  exterior_id?: string;
  skin_id: string;
  display_name?: string;
}

export interface RunResponse {
  ok: boolean;
  job_id?: string;
  cmd?: string[];
  error?: string;
  existing_job_id?: string;
}

async function postJson<T>(url: string, body: unknown): Promise<{ ok: boolean; status: number; body: T }> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let parsed: T;
  try {
    parsed = (await res.json()) as T;
  } catch {
    parsed = {} as T;
  }
  return { ok: res.ok, status: res.status, body: parsed };
}

export function runExtract(body: RunExtractBody): Promise<{ ok: boolean; status: number; body: RunResponse }> {
  return postJson<RunResponse>('/api/extract/run', body);
}

export function runSkinPack(body: RunSkinBody): Promise<{ ok: boolean; status: number; body: RunResponse }> {
  return postJson<RunResponse>('/api/extract/skin', body);
}

interface JobsResponse {
  jobs?: Array<{
    id: string;
    kind: 'extract' | 'skin';
    label: string;
    state: 'running' | 'done' | 'failed' | 'cancelled';
    started_at: number;
    finished_at: number | null;
    exit_code: number | null;
  }>;
}

export async function fetchExtractJobs(): Promise<JobsResponse['jobs']> {
  const body = await fetchJson<JobsResponse>('/api/extract/jobs');
  return body.jobs ?? [];
}

interface OneJobResponse {
  ok?: boolean;
  job?: JobState;
  error?: string;
}

export async function fetchExtractJob(id: string): Promise<JobState | null> {
  const body = await fetchJson<OneJobResponse>(`/api/extract/jobs/${encodeURIComponent(id)}`);
  return body.ok && body.job ? body.job : null;
}

export async function cancelExtractJob(id: string): Promise<JobState | null> {
  const res = await fetch(`/api/extract/jobs/${encodeURIComponent(id)}/cancel`, {
    method: 'POST',
  });
  const body = (await res.json()) as OneJobResponse;
  return body.job ?? null;
}
