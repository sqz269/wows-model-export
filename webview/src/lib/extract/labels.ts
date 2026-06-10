// Pure-function label maps + classifiers for the Extract page. No DOM,
// no Svelte state — testable as plain TS.

import type {
  NativeFilter,
  Permoflage,
  StatusCategory,
  Topology,
  Vehicle,
  VfsIssueStatus,
} from '$lib/types/extract';

/** WG `Vehicle.group` → coarse status category. */
const STATUS_CATEGORY: Record<string, StatusCategory> = {
  upgradeable: 'live',
  special: 'live',
  ultimate: 'live',
  superShip: 'live',
  premium: 'live',
  specialUnsellable: 'live',
  start: 'live',
  demoWithoutStats: 'dev',
  demoWithoutStatsPrem: 'dev',
  experimental: 'dev',
  clan: 'restricted',
  event: 'restricted',
  coopOnly: 'restricted',
  preserved: 'retired',
  disabled: 'retired',
  unavailable: 'retired',
};

export function statusCategory(group: string | null | undefined): StatusCategory {
  if (!group) return 'unknown';
  return STATUS_CATEGORY[group] || 'unknown';
}

/** Class chip order — matches the in-game UI tabs. Extras append in alpha. */
export const CLASS_ORDER = ['DD', 'CA', 'BB', 'CV', 'SS'] as const;

/** Nation key → display label. Keys mirror `GameParams.typeinfo.nation`. */
const NATION_LABELS: Record<string, string> = {
  usa: 'USA',
  japan: 'Japan',
  united_kingdom: 'UK',
  germany: 'Germany',
  france: 'France',
  italy: 'Italy',
  netherlands: 'Netherlands',
  russia: 'USSR',
  spain: 'Spain',
  commonwealth: 'Commonwealth',
  pan_america: 'Pan-Am',
  europe: 'Pan-EU',
  pan_asia: 'Pan-Asia',
  events: 'Events',
  common: 'Common',
};

export function nationLabel(key: string): string {
  if (NATION_LABELS[key]) return NATION_LABELS[key];
  return key
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/** Topology badge labels. */
export const TOPOLOGY_LABELS: Record<Topology, string> = {
  mesh_swap: 'mesh swap',
  mat_albedo: 'mat albedo',
  mat_palette: 'mat palette',
  hull_palette: 'hull palette',
  tile_broadcast: 'tile',
  other: 'other',
};

/** Catch-all peculiarities that match ~70 % of ships — drop from the filter row. */
export const SKIP_PECULIARITY_FILTER = new Set(['decorative', 'default']);

/** Last-resort label when the snapshot doesn't carry a peculiarity entry. */
export function fallbackPeculiarityLabel(key: string): string {
  const parts = key.split('_').filter(Boolean);
  while (parts.length && /^\d+$/.test(parts[parts.length - 1])) parts.pop();
  return parts.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ') || key;
}

/** VFS status badge metadata. `ok` + `unknown` deliberately render nothing —
 *  `ok` is the silent majority, `unknown` fires when the toolkit metadata
 *  dump failed and we don't want to mass-mis-flag healthy ships. */
export const VFS_STATUS_META: Record<
  VfsIssueStatus,
  { label: string; sev: 'warn' | 'fail'; title: string }
> = {
  no_splash: {
    label: 'no splash',
    sev: 'warn',
    title:
      'Hull GLB will export but the .splash file is missing — no hitboxes / damage zones.',
  },
  no_visual: {
    label: 'no visual',
    sev: 'fail',
    title:
      'No .visual file at the expected path — extraction will fail. Often an animation-rig stub, not a real hull.',
  },
  no_dir: {
    label: 'no dir',
    sev: 'fail',
    title:
      'model_dir basename not present in the VFS at all — stale GameParams reference.',
  },
  none: {
    label: 'no model',
    sev: 'fail',
    title: 'Vehicle has no resolvable model_dir in GameParams.',
  },
};

/** Severity-ordered VFS statuses for filter chip rows (warn → fail). */
export const VFS_STATUS_ORDER: VfsIssueStatus[] = [
  'no_splash',
  'no_visual',
  'no_dir',
  'none',
];

/** WG `Vehicle.group` → human-readable filter chip label. Anything not in
 *  this map falls back to a camelCase → "camel case" humanizer so new WG
 *  values still render reasonably. */
const GROUP_LABELS: Record<string, string> = {
  upgradeable: 'upgradeable',
  special: 'special',
  ultimate: 'ultimate',
  superShip: 'super-ship',
  premium: 'premium',
  specialUnsellable: 'special (unsellable)',
  start: 'start',
  demoWithoutStats: 'demo',
  demoWithoutStatsPrem: 'demo (prem)',
  experimental: 'experimental',
  clan: 'clan',
  event: 'event',
  coopOnly: 'coop-only',
  preserved: 'preserved',
  disabled: 'disabled',
  unavailable: 'unavailable',
};

export function groupLabel(key: string): string {
  if (GROUP_LABELS[key]) return GROUP_LABELS[key];
  return key
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/_/g, ' ')
    .toLowerCase();
}

/** Suggested filesystem label for a (Vehicle, Permoflage) pair.
 *
 *  Picking a non-native mesh-swap permoflage (e.g. ARP Takao Red on a
 *  PJSC708 Vehicle whose native is the Blue variant) needs a different
 *  folder than the base extract — otherwise the second extract would
 *  silently overwrite the first. The suffix encodes the permoflage's
 *  identity so each picked variant lands somewhere unique.
 *
 *  Native + non-mesh-swap permoflages get no suffix: native is the
 *  Vehicle's default, and texture-only permoflages share geometry with
 *  the base ship so they belong in `skins[]`, not a sibling folder.
 *
 *  LEGACY (ship-exterior unification): the `__<Variant>` suffix path is
 *  superseded — the BASE ship's sidecar carries every mesh-swap
 *  permoflage as a switchable `exteriors[]` entry (per-mount swaps +
 *  HullDelta variant hull under `models/exteriors/`). The extract UI
 *  warns when this branch fires; the convention retires at cutover
 *  (handoff §4 Step 3 / §9a). */
export function suggestedLabel(v: Vehicle, p: Permoflage | null): string {
  const base = v.display_name.replace(/\s+/g, '_').replace(/[^A-Za-z0-9_]/g, '');
  if (!p || p.is_native || p.topology !== 'mesh_swap') return base;
  const variantSuffix = p.exterior_id
    .replace(/^P[A-Z]{2,3}\d{3,4}_/, '')
    .replace(/[^A-Za-z0-9]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  return variantSuffix ? `${base}__${variantSuffix}` : base;
}

/** PAES329_AZUR_New_Jersey → azur_new_jersey. Good default; user can edit. */
export function suggestedSkinId(exteriorId: string): string {
  const stripped = exteriorId.replace(/^P[A-Z]{2,3}\d{3,4}_/, '');
  return stripped
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

/** Re-export NativeFilter for `<select bind:value>` consumers. */
export type { NativeFilter };

/** `delta` formatter for the GameParams status banner ("3 min ago"). */
export function formatRelativeTime(unixSec: number): string {
  const now = Date.now() / 1000;
  const delta = Math.max(0, now - unixSec);
  if (delta < 60) return `${Math.floor(delta)} s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
  return `${Math.floor(delta / 86400)} d ago`;
}

/** elapsed timer formatter (used by the job panel). */
export function formatElapsed(startedAt: number, finishedAt: number | null): string {
  const end = finishedAt ?? Date.now();
  const ms = end - startedAt;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} m ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} m`;
}
