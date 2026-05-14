// Pure-function filter helpers for the Extract page.
//
// `deriveFilterOptions` produces the chip-row choices from the full vehicles
// array; `filterVehicles` applies the live filter predicate. Both are
// invariant under DOM concerns so they're trivial to test (and to compose
// inside Svelte $derived blocks without churn).

import type {
  ExtractFilterState,
  FilterOptions,
  Vehicle,
} from '$lib/types/extract';
import { CLASS_ORDER, SKIP_PECULIARITY_FILTER, nationLabel } from './labels';

export function defaultFilterState(): ExtractFilterState {
  return {
    text: '',
    showTest: false,
    nation: null,
    classes: new Set(),
    tiers: new Set(),
    peculiarities: new Set(),
    armaments: new Set(),
    native: 'any',
  };
}

/** Derive chip-row choices from the corpus. */
export function deriveFilterOptions(vehicles: Vehicle[]): FilterOptions {
  const nations = new Set<string>();
  const classes = new Set<string>();
  const tiers = new Set<number>();
  const pecCounts: Map<string, number> = new Map();
  const armCounts: Map<string, number> = new Map();

  for (const v of vehicles) {
    if (v.nation) nations.add(v.nation);
    if (v.class) classes.add(v.class);
    if (v.tier != null) tiers.add(v.tier);
    for (const p of v.peculiarities ?? []) {
      if (SKIP_PECULIARITY_FILTER.has(p)) continue;
      pecCounts.set(p, (pecCounts.get(p) ?? 0) + 1);
    }
    for (const a of v.armaments ?? []) {
      armCounts.set(a, (armCounts.get(a) ?? 0) + 1);
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
    // Armament chips: sort by ship-count ASC so RARE types (missiles 9,
    // lasers 2, wave_artillery 2, sub_torpedoes 1) chip up first — the
    // common ones are low-priority noise as filter values.
    armaments: Array.from(armCounts.entries())
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => a.count - b.count || a.key.localeCompare(b.key)),
  };
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
    if (state.classes.size > 0 && !state.classes.has(v.class || '')) return false;
    if (state.tiers.size > 0 && (v.tier == null || !state.tiers.has(v.tier))) {
      return false;
    }
    // Peculiarity: OR semantics — any match passes.
    if (state.peculiarities.size > 0) {
      const pecs = v.peculiarities ?? [];
      let hit = false;
      for (const p of pecs) {
        if (state.peculiarities.has(p)) {
          hit = true;
          break;
        }
      }
      if (!hit) return false;
    }
    if (state.native === 'has' && !v.native_permoflage) return false;
    if (state.native === 'no' && v.native_permoflage) return false;
    // Armament: AND semantics — narrows to ships that have ALL selected.
    if (state.armaments.size > 0) {
      const have = v.armaments ?? [];
      for (const need of state.armaments) {
        if (!have.includes(need)) return false;
      }
    }
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
