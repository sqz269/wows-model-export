// `/api/cleanup` client for the Settings page's "Workspace cleanup"
// section. Two operations:
//
//   GET  /api/cleanup       — inventory: ships on disk, replay plans
//                              recovered, library presence + size.
//                              Drives the confirm dialog's "this will
//                              delete N ships totalling M GB" preview.
//
//   POST /api/cleanup/run   — spawn a job. `mode: "wipe"` tears down
//                              every ship (and optionally the library);
//                              `mode: "reextract"` follows up with a
//                              re-ingest of each recovered ship.
//
// Long-running — the client polls `/api/jobs/{id}` (via the existing
// `waitForJob` helper) to surface progress + the final state.

import { fetchJson } from './client';

export type CleanupMode = 'wipe' | 'reextract';

export interface CleanupShipsStatus {
  /** Ship directories present under `<workspace>/ships/`. */
  on_disk: number;
  /** Sidecars from which we recovered a replay plan. */
  planned: number;
  /** Ship dirs whose sidecar was unparseable or missing the vehicle id —
   *  they'll be torn down but cannot be re-extracted automatically. */
  unrecoverable: string[];
  /** Plans with a `provenance.extract_args` block (lossless replay). */
  stamped: number;
  /** Plans without one — replay falls back to `permoflage="auto"`. */
  fallback: number;
  /** Total skin-pack replays across all recovered ships. */
  total_skins: number;
  /** Approximate size on disk. Walks rglob so allow a second or two. */
  size_mb: number;
}

export interface CleanupLibraryStatus {
  present: boolean;
  size_mb: number;
}

export interface CleanupStatus {
  workspace: string;
  ships: CleanupShipsStatus;
  library: CleanupLibraryStatus;
}

export function fetchCleanupStatus(): Promise<CleanupStatus> {
  return fetchJson<CleanupStatus>('/api/cleanup');
}

export interface CleanupRunBody {
  mode: CleanupMode;
  /** Also rmtree `<workspace>/libraries/accessories/`. Default true. */
  prune_library: boolean;
  /** With `mode: "reextract"`, also re-ingest each ship's skin packs.
   *  Ignored in wipe mode. Default true. */
  replay_skins: boolean;
}

export interface CleanupRunResponse {
  ok: true;
  job_id: string;
  cmd: string[];
}

export function runCleanup(body: CleanupRunBody): Promise<CleanupRunResponse> {
  return fetchJson<CleanupRunResponse>('/api/cleanup/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
