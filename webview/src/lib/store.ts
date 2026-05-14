// Versioned localStorage facade.
//
// One JSON blob under STORE_KEY carries every persisted user preference
// (controls panel state, panel section open/close, ship picker filter,
// etc.). `loadState()` merges the parsed blob over `defaultState()` so
// adding new fields is forward-compatible — older blobs simply pick up
// the default for the new key.
//
// Bumping the version: increment STORE_VERSION (string suffix) and add
// a row to the migration log below. The mirror in the legacy webview
// (`tools/webview/src/ship.ts:3530–3628`) bumped 10 times across the
// project's first year; the log was the only artefact tying schema
// changes to the reason for each change. Keep that habit here.
//
// Migration log:
//   v1 (2026-05-13) — initial schema lifted from tools/webview ship-page.v10
//                     defaults, namespaced to the new repo.

import type { ColorMode, LodPolicy } from './ship';
import type { SeamKey, SeamState, ShipSectionKey } from './types';
import type { RunOptions } from './types/extract';

const STORE_KEY = 'wows-model-export-webview.v1';

export type PanelSection =
  | 'view'
  | 'sections'
  | 'hull-groups'
  | 'damage'
  | 'textures'
  | 'skin';

export interface PersistedState {
  /** Helpers (grid + axes). */
  helpers: boolean;
  /** LOD policy (lod0 / all). */
  lodPolicy: LodPolicy;
  /** Color mode (off / category / hullSection). */
  colorMode: ColorMode;
  /** Per-section visibility — turrets, secondaries, etc. */
  sectionVisible: Record<ShipSectionKey, boolean>;
  /** Per-seam damage state. */
  seamStates: Record<SeamKey, SeamState>;
  /** Force-show patches + cracks (transient, but the user usually
   * wants it sticky across reloads when debugging damage assets). */
  damageVariants: boolean;
  /** Texture pipeline (DDS decoding is expensive — persist the on/off
   * choice so a reload doesn't nuke a costly cache). */
  showTextures: boolean;
  aoMaps: boolean;
  /** Default ON in the new repo — the toolkit emits glTF-conformant
   * `_mr.dds` siblings as of 2026-05-01 (memory
   * `project_raw_dds_mg_unswizzled`) so the underlying signal is
   * correct without the legacy v9 force-off. */
  mrMaps: boolean;
  preserveUnderwater: boolean;
  /** ShipControls panel: which sections are expanded. */
  panelOpen: Record<PanelSection, boolean>;
  /** Ship picker search text. */
  shipSearch: string;
  /** Extract page run-options checkboxes. Persisted so each session
   *  resumes with the user's preferred CLI flags. */
  extractRunOptions: RunOptions;
}

export function defaultState(): PersistedState {
  return {
    helpers: true,
    lodPolicy: 'lod0',
    colorMode: 'off',
    sectionVisible: {
      turrets: true,
      secondaries: true,
      antiair: true,
      torpedoes: true,
      accessories: true,
    },
    seamStates: {
      'Bow-MidFront': 'Intact',
      'MidFront-MidBack': 'Intact',
      'MidBack-Stern': 'Intact',
    },
    damageVariants: false,
    showTextures: false,
    aoMaps: true,
    mrMaps: true,
    preserveUnderwater: true,
    panelOpen: {
      view: true,
      sections: true,
      'hull-groups': false,
      damage: false,
      textures: false,
      skin: true,
    },
    shipSearch: '',
    extractRunOptions: {
      build_library: true,
      and_publish: false,
      publish_force: false,
    },
  };
}

/** Read the blob from localStorage, deep-merged over `defaultState()`. */
export function loadState(): PersistedState {
  if (typeof window === 'undefined' || !window.localStorage) return defaultState();
  const raw = window.localStorage.getItem(STORE_KEY);
  if (!raw) return defaultState();
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedState>;
    return mergeWithDefaults(parsed);
  } catch (err) {
    console.warn('[store] failed to parse persisted state — resetting:', err);
    return defaultState();
  }
}

/** Persist the full state blob. Safe to call frequently — coalesces are
 * the caller's responsibility (most call sites only persist on user
 * action, so even raw frequency is fine for human-rate input). */
export function saveState(state: PersistedState): void {
  if (typeof window === 'undefined' || !window.localStorage) return;
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(state));
  } catch (err) {
    // Quota errors, private-window restrictions, etc. Non-fatal.
    console.warn('[store] failed to persist state:', err);
  }
}

/** Patch helper: load → merge → save. Returns the new full state for
 * callers that want to reuse the merged value without re-reading. */
export function patchState(patch: Partial<PersistedState>): PersistedState {
  const next = { ...loadState(), ...patch };
  saveState(next);
  return next;
}

/** Update a single nested record (sectionVisible, seamStates, panelOpen)
 * without clobbering sibling keys. */
export function patchNestedState<
  K extends 'sectionVisible' | 'seamStates' | 'panelOpen',
>(key: K, patch: Partial<PersistedState[K]>): PersistedState {
  const current = loadState();
  const next: PersistedState = {
    ...current,
    [key]: { ...current[key], ...patch },
  };
  saveState(next);
  return next;
}

function mergeWithDefaults(parsed: Partial<PersistedState>): PersistedState {
  const d = defaultState();
  // Shallow merge for top-level, deep merge for the three nested record
  // keys. Anything missing in the parsed blob keeps the default — the
  // whole point of the forward-compat pattern.
  return {
    ...d,
    ...parsed,
    sectionVisible: { ...d.sectionVisible, ...(parsed.sectionVisible ?? {}) },
    seamStates: { ...d.seamStates, ...(parsed.seamStates ?? {}) },
    panelOpen: { ...d.panelOpen, ...(parsed.panelOpen ?? {}) },
    extractRunOptions: { ...d.extractRunOptions, ...(parsed.extractRunOptions ?? {}) },
  };
}
