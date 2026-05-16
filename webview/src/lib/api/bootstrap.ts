// `/api/bootstrap` client + job polling for the Settings page's
// workspace-artifact builds.
//
// Two prereqs today (`snapshot`, `library`). GET returns presence +
// freshness for each; POST spawns the matching CLI as a job. The
// returned job_id is polled through `/api/jobs/{id}` (kind-neutral
// alias of the legacy /extract/jobs URL).

import { fetchJson } from './client';

export type BootstrapTarget = 'snapshot' | 'library';

export interface BootstrapTargetStatus {
  label: string;
  description: string;
  job_label: string;
  /** The exact argv the backend will spawn. Surfaced verbatim so the
   *  Settings page can show "Running: wows-snapshot --output …" in
   *  the log header. */
  cmd: string[];
  /** Config fields that must be set before this build can run. The UI
   *  disables the Build button when any are missing. */
  requires_config: string[];
  path: string;
  present: boolean;
  mtime_ms: number | null;
  size_bytes: number | null;
}

export interface BootstrapStatus {
  workspace: string;
  /** True iff game_dir AND toolkit_bin are resolved. */
  config_complete: boolean;
  missing_config: string[];
  targets: Record<BootstrapTarget, BootstrapTargetStatus>;
}

export function fetchBootstrap(): Promise<BootstrapStatus> {
  return fetchJson<BootstrapStatus>('/api/bootstrap');
}

export interface BootstrapBuildResponse {
  ok: true;
  job_id: string;
  cmd: string[];
}

export function buildBootstrapTarget(
  target: BootstrapTarget,
): Promise<BootstrapBuildResponse> {
  return fetchJson<BootstrapBuildResponse>('/api/bootstrap/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target }),
  });
}

export interface BootstrapResetResponse {
  ok: true;
  /** Echo of the target that was reset. */
  target: BootstrapTarget;
  /** Absolute path of the directory the backend wiped. */
  path: string;
  /** False when the directory was already absent (still a 200, idempotent). */
  existed: boolean;
}

/** Wipe a bootstrap target's on-disk state. Synchronous on the server
 *  (rmtree, no job runner). 409 if a build for the same target is in
 *  flight. After this resolves, fetchBootstrap() reports present=false
 *  and the Build button rebuilds from scratch. */
export function resetBootstrapTarget(
  target: BootstrapTarget,
): Promise<BootstrapResetResponse> {
  return fetchJson<BootstrapResetResponse>('/api/bootstrap/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target }),
  });
}

// ── Job polling (generic) ───────────────────────────────────────────

export type JobState = 'running' | 'done' | 'failed' | 'cancelled';

export interface JobDetail {
  id: string;
  kind: string;
  label: string;
  state: JobState;
  cmd: string[];
  started_at: number;
  finished_at: number | null;
  exit_code: number | null;
  stdout: string;
  stderr: string;
}

export function fetchJob(jobId: string): Promise<JobDetail> {
  return fetchJson<JobDetail>(`/api/jobs/${encodeURIComponent(jobId)}`);
}

/**
 * Poll a job until it leaves the `running` state. Calls `onTick` with
 * the latest snapshot after each poll (useful for streaming log
 * output). Resolves with the terminal state. Caller is responsible
 * for cancelling via `cancelJob(id)` if they want to bail early —
 * this helper just polls.
 */
export async function waitForJob(
  jobId: string,
  onTick?: (j: JobDetail) => void,
  intervalMs = 750,
): Promise<JobDetail> {
  // Backoff would be nice but the existing extract code polls at a
  // fixed 750ms cadence and the user is staring at the page — fast
  // enough for the bootstrap UI's purposes.
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const j = await fetchJob(jobId);
    onTick?.(j);
    if (j.state !== 'running') return j;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
