// Extract page types. Mirror the snapshot.py JSON contract; the consumer
// helpers (label maps, classifiers, regex predicates) live in
// `$lib/extract/labels` so this file stays a pure shape definition.

export type VfsStatus = 'ok' | 'no_splash' | 'no_visual' | 'no_dir' | 'none' | 'unknown';

/** Actionable VFS issues (drops the silent `ok` + the metadata-failed `unknown`).
 *  Used by filter chips and the row badge, which never render those two. */
export type VfsIssueStatus = Exclude<VfsStatus, 'ok' | 'unknown'>;

export type StatusCategory = 'live' | 'dev' | 'restricted' | 'retired' | 'unknown';

export type Topology =
  | 'mesh_swap'
  | 'mat_albedo'
  | 'mat_palette'
  | 'hull_palette'
  | 'tile_broadcast'
  | 'other';

export type NativeFilter = 'any' | 'has' | 'no';

export type SkinSource = 'wg' | 'vfs' | 'loose';

export interface Vehicle {
  param_index: string;
  top_key: string;
  display_name: string;
  model_dir: string | null;
  tier: number | null;
  nation: string | null;
  species: string | null;
  class: string | null;
  is_premium: boolean;
  is_in_test: boolean;
  is_paper: boolean;
  native_permoflage: string | null;
  permoflages_count: number;
  shares_model_dir_with: string[];
  /** Distinct `peculiarity` values across this Vehicle's permoflages. */
  peculiarities?: string[];
  /** WG `Vehicle.group` — discriminates live ships from dev/clan/event/etc. */
  group?: string | null;
  /** Canonical armament tags (main / aa / torpedoes / missiles / …). Still
   *  emitted by the snapshot producer but no longer surfaced in the picker. */
  armaments?: string[];
  /** VFS extraction-readiness — `ok` / `no_splash` / `no_visual` / `no_dir` / `none` / `unknown`. */
  vfs_status?: VfsStatus;
}

export interface CategoryTextureEntry {
  /** Workspace-relative path under `libraries/camo_mat/` or `libraries/camo_masks/`. */
  lib_path: string;
  kind: 'atlas' | 'mask';
}

export interface Permoflage {
  exterior_id: string;
  display_name: string;
  camouflage: string | null;
  peculiarity: string | null;
  topology: Topology;
  is_native: boolean;
  mesh_swap_dir: string | null;
  /** Per-category overlays for the three structurally interesting topologies
   *  (mat_palette / hull_palette / tile_broadcast). Drives the per-category
   *  preview in the picker info card. */
  category_textures?: Record<string, CategoryTextureEntry> | null;
}

export interface PeculiarityLabel {
  label: string;
  source: 'override' | 'single' | 'prefix' | 'firstword' | 'phrase' | 'humanize';
  sample_names: string[];
  exterior_count: number;
}

export interface SnapshotResponse {
  vehicles?: Vehicle[];
  permoflages_by_vehicle?: Record<string, Permoflage[]>;
  peculiarity_labels?: Record<string, PeculiarityLabel>;
  summary?: {
    vehicle_count?: number;
    permoflage_count?: number;
    ships_with_permoflages?: number;
  };
  error?: string;
  ok?: boolean;
  stderr?: string;
}

export interface GpStatus {
  exists: boolean;
  path: string;
  size_mb?: number;
  mtime?: number;
  mtime_iso?: string;
  hint?: string;
}

export type JobKind = 'extract' | 'skin' | 'bootstrap' | 'rig' | 'consumer' | 'cleanup';

export interface JobState {
  id: string;
  kind: JobKind;
  label: string;
  state: 'running' | 'done' | 'failed' | 'cancelled';
  cmd: string[];
  started_at: number;
  finished_at: number | null;
  exit_code: number | null;
  stdout: string;
  stderr: string;
}

/** Already-extracted ship summary — drives the skin-pack panel's target dropdown. */
export interface ExtractedShip {
  name: string;
  display_name: string;
  nation: string | null;
  ship_class: string | null;
  tier: number | null;
}

export interface SkinPackForm {
  ship: string;
  source: SkinSource;
  source_arg: string;
  exterior_id: string;
  skin_id: string;
  display_name: string;
}

/** Tri-state chip filter: off (in neither set), include (in `include`),
 *  or exclude (in `exclude`). Clicking a chip cycles off → include → exclude
 *  → off. Predicates apply include as a whitelist (any match passes) and
 *  exclude as a blacklist (any match drops the ship). */
export interface ChipFilter<T> {
  include: Set<T>;
  exclude: Set<T>;
}

export interface ExtractFilterState {
  text: string;
  showTest: boolean;
  nation: string | null;
  classes: ChipFilter<string>;
  tiers: ChipFilter<number>;
  peculiarities: ChipFilter<string>;
  /** WG `Vehicle.group` (disabled / unavailable / clan / …). */
  groups: ChipFilter<string>;
  /** Extraction-readiness (no_splash / no_visual / no_dir / none). */
  vfsStatuses: ChipFilter<VfsIssueStatus>;
  native: NativeFilter;
}

export interface FilterOptions {
  nations: string[];
  classes: string[];
  tiers: number[];
  peculiarities: { key: string; count: number }[];
  /** WG group values, ranked by ship-count DESC. */
  groups: { key: string; count: number }[];
  /** Non-ok/unknown VFS statuses, in severity order. */
  vfsStatuses: { key: VfsIssueStatus; count: number }[];
}
