// Pure-function filter helpers for the Extract page.
//
// `deriveFilterOptions` produces the chip-row choices from the full vehicles
// array; `filterVehicles` applies the live filter predicate. Both are
// invariant under DOM concerns so they're trivial to test (and to compose
// inside Svelte $derived blocks without churn).

import type {
  ChipFilter,
  ExtractFilterState,
  FilterOptions,
  Vehicle,
  VfsIssueStatus,
} from '$lib/types/extract';
import {
  CLASS_ORDER,
  SKIP_PECULIARITY_FILTER,
  VFS_STATUS_ORDER,
  nationLabel,
} from './labels';

export type ChipState = 'off' | 'include' | 'exclude';

function emptyChip<T>(): ChipFilter<T> {
  return { include: new Set<T>(), exclude: new Set<T>() };
}

export function defaultFilterState(): ExtractFilterState {
  return {
    text: '',
    showTest: false,
    nation: null,
    classes: emptyChip(),
    tiers: emptyChip(),
    peculiarities: emptyChip(),
    groups: emptyChip(),
    vfsStatuses: emptyChip(),
    native: 'any',
  };
}

/** Resolve a value's tri-state position within a ChipFilter. */
export function chipState<T>(filter: ChipFilter<T>, value: T): ChipState {
  if (filter.include.has(value)) return 'include';
  if (filter.exclude.has(value)) return 'exclude';
  return 'off';
}

/** Cycle a chip: off → include → exclude → off. Returns a new ChipFilter
 *  so callers can spread it back into the reactive filter-state object. */
export function cycleChip<T>(filter: ChipFilter<T>, value: T): ChipFilter<T> {
  const include = new Set(filter.include);
  const exclude = new Set(filter.exclude);
  if (include.has(value)) {
    include.delete(value);
    exclude.add(value);
  } else if (exclude.has(value)) {
    exclude.delete(value);
  } else {
    include.add(value);
  }
  return { include, exclude };
}

export function chipFilterIsActive<T>(filter: ChipFilter<T>): boolean {
  return filter.include.size > 0 || filter.exclude.size > 0;
}

export function chipFilterActiveCount<T>(filter: ChipFilter<T>): number {
  return filter.include.size + filter.exclude.size;
}

/** Derive chip-row choices from the corpus. */
export function deriveFilterOptions(vehicles: Vehicle[]): FilterOptions {
  const nations = new Set<string>();
  const classes = new Set<string>();
  const tiers = new Set<number>();
  const pecCounts: Map<string, number> = new Map();
  const groupCounts: Map<string, number> = new Map();
  const vfsCounts: Map<VfsIssueStatus, number> = new Map();

  for (const v of vehicles) {
    if (v.nation) nations.add(v.nation);
    if (v.class) classes.add(v.class);
    if (v.tier != null) tiers.add(v.tier);
    for (const p of v.peculiarities ?? []) {
      if (SKIP_PECULIARITY_FILTER.has(p)) continue;
      pecCounts.set(p, (pecCounts.get(p) ?? 0) + 1);
    }
    if (v.group) {
      groupCounts.set(v.group, (groupCounts.get(v.group) ?? 0) + 1);
    }
    // `ok` is the silent majority and `unknown` means metadata-dump failed —
    // neither is actionable as a filter chip.
    const vs = v.vfs_status;
    if (vs && vs !== 'ok' && vs !== 'unknown') {
      vfsCounts.set(vs, (vfsCounts.get(vs) ?? 0) + 1);
    }
  }

  // Class chips: conventional in-game tab order first; extras append alpha.
  const knownClasses = CLASS_ORDER.filter((c) => classes.has(c));
  const extras = Array.from(classes)
    .filter((c) => !(CLASS_ORDER as readonly string[]).includes(c))
    .sort();

  return {
    nations: Array.from(nations).sort((a, b) =>
      nationLabel(a).localeCompare(nationLabel(b)),
    ),
    classes: [...knownClasses, ...extras],
    tiers: Array.from(tiers).sort((a, b) => a - b),
    // Peculiarity chips: sort by ship-count DESC so the most useful
    // values (Halloween, Azur Lane, Arpeggio, …) chip up first.
    peculiarities: Array.from(pecCounts.entries())
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
    // Group chips: sort by ship-count DESC so dominant values (upgradeable,
    // premium, special) lead and rare ones (disabled, unavailable) follow.
    groups: Array.from(groupCounts.entries())
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
    // VFS chips: stable severity order (warn → fail), counts purely tooltip.
    vfsStatuses: VFS_STATUS_ORDER.filter((k) => vfsCounts.has(k)).map((key) => ({
      key,
      count: vfsCounts.get(key) ?? 0,
    })),
  };
}

/** Single-value chip filter: include = whitelist; exclude = blacklist.
 *  Returns false when the vehicle should be dropped. */
function passSingleChip<T>(
  filter: ChipFilter<T>,
  value: T | null | undefined,
): boolean {
  if (filter.exclude.size > 0 && value != null && filter.exclude.has(value)) {
    return false;
  }
  if (filter.include.size > 0) {
    if (value == null || !filter.include.has(value)) return false;
  }
  return true;
}

/** Multi-value chip filter: include = whitelist (any match passes);
 *  exclude = blacklist (any match drops). */
function passMultiChip<T>(filter: ChipFilter<T>, values: readonly T[]): boolean {
  if (filter.exclude.size > 0) {
    for (const v of values) {
      if (filter.exclude.has(v)) return false;
    }
  }
  if (filter.include.size > 0) {
    let hit = false;
    for (const v of values) {
      if (filter.include.has(v)) {
        hit = true;
        break;
      }
    }
    if (!hit) return false;
  }
  return true;
}

/** Apply the filter predicate to a vehicles array. */
export function filterVehicles(
  vehicles: Vehicle[],
  state: ExtractFilterState,
): Vehicle[] {
  const q = state.text.trim().toLowerCase();
  return vehicles.filter((v) => {
    if (!state.showTest && v.is_in_test) return false;
    if (state.nation && v.nation !== state.nation) return false;
    if (!passSingleChip(state.classes, v.class)) return false;
    if (!passSingleChip(state.tiers, v.tier)) return false;
    if (!passMultiChip(state.peculiarities, v.peculiarities ?? [])) return false;
    if (state.native === 'has' && !v.native_permoflage) return false;
    if (state.native === 'no' && v.native_permoflage) return false;
    if (!passSingleChip(state.groups, v.group ?? null)) return false;
    // VFS narrows `ok` + `unknown` out of the chip universe; treat them as
    // null so they neither match include nor exclude.
    const vs = v.vfs_status;
    const vsKey: VfsIssueStatus | null =
      vs && vs !== 'ok' && vs !== 'unknown' ? vs : null;
    if (!passSingleChip(state.vfsStatuses, vsKey)) return false;
    if (!q) return true;
    return (
      v.top_key.toLowerCase().includes(q) ||
      v.param_index.toLowerCase().includes(q) ||
      (v.model_dir?.toLowerCase().includes(q) ?? false)
    );
  });
}

/** Bucket vehicles by model_dir for the expandable group view. Preserves
 *  the input order within each bucket. */
export function groupByModelDir(
  vehicles: Vehicle[],
): { modelDir: string; vehicles: Vehicle[] }[] {
  const buckets: Map<string, Vehicle[]> = new Map();
  const orderedKeys: string[] = [];
  for (const v of vehicles) {
    const md = v.model_dir || '__no_model_dir__';
    if (!buckets.has(md)) {
      buckets.set(md, []);
      orderedKeys.push(md);
    }
    buckets.get(md)!.push(v);
  }
  return orderedKeys.map((md) => ({ modelDir: md, vehicles: buckets.get(md)! }));
}
