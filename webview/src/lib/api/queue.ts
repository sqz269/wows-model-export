// `/api/queue` client for the persistent extract queue.
//
// The backend holds the queue + a single worker thread that runs items
// sequentially via the existing `spawn_job(kind="extract", ...)` path.
// From the client's perspective: enqueue an item here, poll
// `fetchQueue()` for ordering + status, and (when there's a running
// item) poll the regular `/api/jobs/{id}` for live log output via the
// existing job-polling helpers.
//
// Wire-shape note: a queue's running item shares the same `job_id` as
// the underlying job, so `ExtractJobPanel` reuses untouched.

import { fetchJson } from './client';

export type QueueStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled';

export interface QueueItem {
  queue_id: string;
  /** GameParams top_key (e.g. "PASB018_Iowa_1944"). */
  vehicle: string;
  /** Filesystem label (matches <workspace>/ships/<label>/). */
  label: string;
  /** "auto" | "none" | explicit exterior_id. */
  permoflage: string | null;
  build_library: boolean;
  /** Pre-resolved kwargs the worker hands to compose.ingest_ship. */
  toolkit_ship: string;
  gameparams_ship_id: string;

  status: QueueStatus;
  /** Bound by the worker once it dispatches; null while pending. */
  job_id: string | null;
  enqueued_at: number;
  started_at: number | null;
  finished_at: number | null;
  /** Short error blurb; full traceback lives in the underlying job. */
  error: string | null;
}

export interface QueueSnapshot {
  items: QueueItem[];
  paused: boolean;
  /** Convenience: the running item's job_id (or null). Lets the UI
   *  drive its job-poll loop without re-scanning items[]. */
  running_job_id: string | null;
  pending_count: number;
  completed_count: number;
}

export function fetchQueue(): Promise<QueueSnapshot> {
  return fetchJson<QueueSnapshot>('/api/queue');
}

export interface EnqueueExtractBody {
  vehicle: string;
  label: string;
  permoflage: string | null;
  build_library: boolean;
  /** HullDelta: export hull-swap exteriors' variant hull GLBs during the
   *  scaffold (base ingests only — no-op on variant-routed extracts). */
  exterior_hulls?: boolean;
}

export interface EnqueueResponse {
  ok: true;
  queue_id: string;
}

export function enqueueExtract(body: EnqueueExtractBody): Promise<EnqueueResponse> {
  return fetchJson<EnqueueResponse>('/api/queue/enqueue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export interface QueueItemMutationResponse {
  ok: true;
  queue_id: string;
  status: QueueStatus;
}

/** Drop a pending item, or cancel a running one. Terminal-state items
 *  are removed from the displayed list. */
export function dropQueueItem(queueId: string): Promise<QueueItemMutationResponse> {
  return fetchJson<QueueItemMutationResponse>(
    `/api/queue/${encodeURIComponent(queueId)}`,
    { method: 'DELETE' },
  );
}

export interface ReorderResponse {
  ok: true;
  reordered: number;
}

/** Reorder the pending tail. Non-pending ids in `order` are ignored;
 *  pending ids not in `order` keep their relative order at the end. */
export function reorderQueue(order: string[]): Promise<ReorderResponse> {
  return fetchJson<ReorderResponse>('/api/queue/reorder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order }),
  });
}

export interface ClearCompletedResponse {
  ok: true;
  dropped: number;
}

export function clearCompletedQueue(): Promise<ClearCompletedResponse> {
  return fetchJson<ClearCompletedResponse>('/api/queue/clear-completed', {
    method: 'POST',
  });
}

export interface PauseResponse {
  ok: true;
  paused: boolean;
}

export function pauseQueue(): Promise<PauseResponse> {
  return fetchJson<PauseResponse>('/api/queue/pause', { method: 'POST' });
}

export function resumeQueue(): Promise<PauseResponse> {
  return fetchJson<PauseResponse>('/api/queue/resume', { method: 'POST' });
}
