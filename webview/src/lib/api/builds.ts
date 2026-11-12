// `/api/builds` client for the Updates page: what changed between game
// builds, and which exports a game update made stale.
//
//   GET  /api/builds            builds on disk, saved manifests, cache stamps,
//                               every export's state vs the current build
//   GET  /api/builds/diff       build-to-build diff summary (?from=&to=)
//   POST /api/builds/snapshot   job: save the current build's manifest
//   POST /api/builds/reexport   job: re-run stale / named exports

import { fetchJson } from './client';

/** `removed`: the ship or map space is gone from the current build. */
export type ExportState = 'current' | 'stale' | 'untracked' | 'error' | 'removed';
export type ExportKind = 'ship' | 'map' | 'prop_hull';
export type BuildSection = 'files' | 'records' | 'gameparams' | 'camouflages';

export interface BuildCacheStatus {
  name: string;
  exists: boolean;
  /** Build the cache was made from (null = made before stamps existed). */
  build: number | null;
  current: boolean;
}

export interface ExportStatus {
  kind: ExportKind;
  name: string;
  path: string;
  state: ExportState;
  /** Game build the export was made from (null when untracked). */
  build: number | null;
  exported_at: string | null;
  changed_count: number;
  /** Changed inputs per section (capped server-side). */
  changed: Partial<Record<BuildSection, string[]>>;
  /** Inputs that no longer exist in the current build. */
  removed: Partial<Record<BuildSection, string[]>>;
  counts: Partial<Record<BuildSection, number>>;
  /** Arguments a re-export replays. */
  reexport: Record<string, unknown>;
  note: string;
  /** Sections not checked because the hashing changed since export. */
  incomparable: BuildSection[];
}

export interface BuildsStatus {
  current_build: number | null;
  builds_with_idx: number[];
  stored_manifests: number[];
  caches: BuildCacheStatus[];
  /** The current build has no saved manifest yet: run a snapshot first. */
  needs_snapshot: boolean;
  /** Null while `needs_snapshot`. */
  exports: ExportStatus[] | null;
  counts: Partial<Record<ExportState, number>>;
  /** Game-build state of the shared accessory library (null while
   *  `needs_snapshot`). Stale and untracked assets are rebuilt by the
   *  library build at the end of a re-export. */
  library: LibraryStatus | null;
}

export interface LibraryStatus {
  current?: number;
  /** Assets with no build record (exported before tracking). */
  untracked?: number;
  stale_count?: number;
  /** Library GLBs no ship in the workspace uses (left by removed ships). */
  unused?: number;
  stale?: { asset_id: string; reason: string }[];
  error?: string;
}

export interface CappedList {
  total: number;
  items: string[];
}

export interface SectionCounts {
  added: number;
  removed: number;
  changed: number;
}

export interface BuildDiff {
  from_build: number;
  to_build: number;
  summary: {
    files: SectionCounts;
    records: SectionCounts;
    gameparams: SectionCounts;
    camouflages: SectionCounts;
    records_changed_by_type: Record<string, number>;
    shallow_records: number;
  };
  by_area: {
    files_changed: Record<string, number>;
    files_added: Record<string, number>;
    records_changed: Record<string, number>;
    records_added: Record<string, number>;
  };
  /** Groups where nearly every file of a kind changed: likely a format change. */
  format_signals: { kind: string; changed: number; total: number }[];
  /** Sections whose hashing differs between the two manifests. */
  incomparable: BuildSection[];
  /** Space name -> {added|removed|changed: file count}. */
  spaces: Record<string, Partial<Record<'added' | 'removed' | 'changed', number>>>;
  /** Ship model dir -> changed/added file + record count. */
  ship_model_dirs: Record<string, number>;
  particles: CappedList;
  gameparams: Record<'added' | 'removed' | 'changed', CappedList>;
  camouflages: Record<'added' | 'removed' | 'changed', CappedList>;
  sections: { from: Record<string, string>; to: Record<string, string> };
}

export function fetchBuildsStatus(): Promise<BuildsStatus> {
  return fetchJson<BuildsStatus>('/api/builds');
}

export function fetchBuildDiff(from?: number, to?: number): Promise<BuildDiff> {
  const q = new URLSearchParams();
  if (from) q.set('from', String(from));
  if (to) q.set('to', String(to));
  const qs = q.toString();
  return fetchJson<BuildDiff>(`/api/builds/diff${qs ? `?${qs}` : ''}`);
}

export interface BuildsJobResponse {
  ok: true;
  job_id: string;
  cmd: string[];
}

export function runBuildSnapshot(refresh = false): Promise<BuildsJobResponse> {
  return fetchJson<BuildsJobResponse>('/api/builds/snapshot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh }),
  });
}

export interface ReexportBody {
  /** Only these exports (ship label / map name). Empty = all stale. */
  names?: string[];
  kind?: ExportKind | null;
  include_untracked?: boolean;
  replay_skins?: boolean;
  build_library?: boolean;
  /** Exports to run at once (server default 3). */
  jobs?: number;
}

export function runReexport(body: ReexportBody): Promise<BuildsJobResponse> {
  return fetchJson<BuildsJobResponse>('/api/builds/reexport', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// ── Build picker + build archive ────────────────────────────────────────
//
//   GET  /api/builds/sources   every readable game build (install + archive)
//                              and which one this browser views
//   GET  /api/builds/archive   archive vs install + the scheduled task
//   POST /api/builds/archive   job: copy what the archive lacks
//
// The viewed build is the `wows_build` cookie: the backend serves every
// /api and /repo request carrying it from that build's workspace
// (`<workspace>/builds/<build>/`). No cookie = the install's current build.

export interface BuildSource {
  build: number;
  /** The build the install runs now (its workspace is the plain one). */
  current: boolean;
  in_install: boolean;
  in_archive: boolean;
  /** Where a view of this build reads its game data (null: unreadable). */
  reads: 'install' | 'archive' | null;
  workspace: string;
  workspace_exists: boolean;
  exports: { ships: number; maps: number };
}

export interface BuildSources {
  current_build: number | null;
  /** The build this browser views (the current build without a cookie). */
  selected: number | null;
  cookie: string;
  /** The cookie's build, even when it can't be served. */
  requested: number | null;
  archive: { dir: string | null; builds: number[] };
  /** Newest first. */
  builds: BuildSource[];
}

export function fetchBuildSources(): Promise<BuildSources> {
  return fetchJson<BuildSources>('/api/builds/sources');
}

export const BUILD_COOKIE = 'wows_build';

/** View `build` in this browser (`null`: the current build). The caller
 *  reloads the page so every tab refetches from that build's workspace. */
export function setViewedBuild(build: number | null): void {
  if (build === null) {
    document.cookie = `${BUILD_COOKIE}=; path=/; max-age=0; SameSite=Lax`;
  } else {
    document.cookie = `${BUILD_COOKIE}=${build}; path=/; max-age=31536000; SameSite=Lax`;
  }
}

export interface ArchiveFileDelta {
  path: string;
  size: number;
  reason: 'missing' | 'size' | 'newer';
}

export interface ArchivePlan {
  game_dir: string;
  archive_dir: string;
  install_builds: number[];
  archived_builds: number[];
  up_to_date: boolean;
  unarchived_builds: number[];
  builds: {
    build: number;
    files: number;
    in_archive: boolean;
    archived: boolean;
    to_copy: number;
    bytes_to_copy: number;
    deltas: ArchiveFileDelta[];
  }[];
  packages: { install: number; to_copy: number; bytes_to_copy: number; deltas: ArchiveFileDelta[] };
  bytes_to_copy: number;
  newest_change_age_s: number | null;
}

export interface ArchiveStatus {
  configured: boolean;
  dir: string | null;
  plan?: ArchivePlan;
  error?: string;
  /** The scheduled archive task (null: not registered). */
  schedule?: {
    task: string;
    state: string;
    last_run: string | null;
    last_result: number | null;
    next_run: string | null;
    command: string;
  } | null;
}

export function fetchArchiveStatus(): Promise<ArchiveStatus> {
  return fetchJson<ArchiveStatus>('/api/builds/archive');
}

/** `force`: copy even if the install changed in the last 30 minutes. */
export function runArchive(force = false): Promise<BuildsJobResponse> {
  return fetchJson<BuildsJobResponse>('/api/builds/archive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });
}
