// Texture pipeline orchestrator. Owns every texture-related cache, the
// two-sided bind state, the active skin, and the per-clone uniform push.
// ShipViewer holds one TextureManager and delegates all texture/skin
// operations to it; the manager knows about Three.js but nothing about
// Svelte / the SPA shell.
//
// Public methods:
//   • registerMesh / bindSchemes — building the bind index (called during
//     hull classification + accessory placement)
//   • setShowTextures / setActiveSkin / setSkinTable — runtime toggles
//   • setAoEnabled / setMrMapEnabled — diagnostics
//   • clearShip / dispose — lifecycle
//
// Two-sided binding pattern: meshes register before the slot URLs are
// known (hull during classifyHullScene, accessories during clone), so
// `bindSchemes` retroactively populates already-registered entries via
// `entriesByKey`, and `registerMesh` retroactively reads any
// already-bound schemes via `schemesByKey`. Same key from both sides.

import type * as THREE from 'three';
import { resolveDdsMipUrls } from '$lib/dds';
import { classifyPartCategory, classifyPlacementCategory } from '$lib/types';
import type { ShipPlacement, Skin, SidecarDoc, SidecarTextureScheme, SkinMatCategoryParams, TextureSet } from '$lib/types';
import { dummyMaskTexture, dummyMatAlbedoTexture, uniformsOf } from '../camo';
import {
  applyTexturesToMaterial,
  buildTextured,
  type DetailParams,
  type MaterialClonePolicy,
} from './material';
import { CategoryMaskCache, MatAlbedoCache, MgnTextureCache } from './category_mask';
import { DecodedTextureCache } from './decode';
import type { SlotUrls, TextureMeshEntry, TextureSetResolved } from './types';

/**
 * Snapshot of the camo binding + per-entry state for the active skin.
 * Returned by `TextureManager.getCamoDiagnostics()` and consumed by the
 * ship inspector's debug panel.
 */
export interface CamoDiagnostics {
  activeSkinId: string | null;
  schemeKey: string;
  /** Palette colors (RGBA, 0-1) from `skin.color_scheme.colors`. Null
   *  when the active skin has no palette (e.g. default/main). */
  paletteColors: number[][] | null;
  /** Union of `skin.categories` and `skin.mat_textures` keys, with a
   *  per-binding summary. `tile/deckhouse/bulge` are hull-side; the rest
   *  (gun/director/plane/float/misc/wire) are accessory categories. */
  categories: Record<string, {
    hasMask: boolean;
    hasMgn: boolean;
    hasMatAlbedo: boolean;
    uvScale?: [number, number];
    uvOffset?: [number, number];
  }>;
  entryStats: {
    total: number;
    hullEntries: number;
    accessoryEntries: number;
    camoEnabled: number;
    matAlbedoEnabled: number;
    /** Both paths off — entry has a textured clone but no paint applied. */
    bothDisabled: number;
    /** Entries whose binding key is in `noCamoKeys` (sidecar
     *  `shader_intent: "transparent"`). The camo chunk is still attached
     *  to their clones but the dispatch forces all uniforms to disabled. */
    noCamoEntries: number;
  };
  perCategory: Record<string, { total: number; camoOn: number; matOn: number }>;
  /** Full list of binding keys with sidecar `shader_intent: "transparent"`. */
  noCamoKeys: string[];
}

const SLOTS: (keyof TextureSet)[] = [
  'baseColor',
  'metallicRoughness',
  'normal',
  'occlusion',
  'emissive',
  'camoMask',
  'camoExclusionMask',
  'detail',
];

const PARALLEL = 8;

export interface TextureManagerInit {
  renderer: THREE.WebGLRenderer;
  /**
   * Called when an accessory mesh's material gets swapped, so the
   * ShipViewer can mirror the swap onto `placementColorEntries[].originalMaterial`
   * (otherwise color-mode 'off' would snap back to the untextured material).
   */
  onAccessoryMaterialSwap: (mesh: THREE.Mesh, mat: THREE.Material | THREE.Material[]) => void;
  /**
   * Called after texture state changes so the ShipViewer can re-apply
   * the current color mode (a category-mode toggle after a texture swap
   * needs to pick up the new `originalMaterial`).
   */
  onAfterTextureApply: () => void;
}

export class TextureManager {
  private renderer: THREE.WebGLRenderer;
  private repoBaseUrl: string;

  private entries: TextureMeshEntry[] = [];
  private schemesByKey = new Map<string, Map<string, SlotUrls>>();
  private entriesByKey = new Map<string, TextureMeshEntry[]>();
  // Binding keys whose sidecar `shader_intent` says "transparent". Engine
  // analog: those materials carry no enumerated name in the part_index
  // lookup at exe 0x140071a20 so `FUN_14108c360` bails out before
  // `makeCamoMaterial` is invoked. Checked at dispatch + material-build
  // time against `entry.key` (no per-entry cache → no retroactive flip
  // needed when bind/register orderings vary).
  private noCamoKeys = new Set<string>();
  // Binding keys whose sidecar `shader_intent` says "cutout". Used to
  // flip `alphaTest = 0.5` on the textured clone (mirrors `noCamoKeys`'s
  // role for `"transparent"`). WG library shaders 0x00010000 (nets /
  // grids / fences — single-map alpha-tested) and friends ship through
  // the toolkit's glTF as `alphaMode: Opaque`, so the GLTFLoader leaves
  // alpha-test off — without this flag the holes render as solid panels.
  private cutoutKeys = new Set<string>();
  // Per-key detail-normal blend params from the sidecar's
  // `materials[*].detail_params`. Read at material-build time and
  // pushed into the camo shader chunk's `detail*` uniforms. Absent
  // entries → detail disabled (`detailMapBound = 0.0`).
  private detailParamsByKey = new Map<string, DetailParams>();

  private decodedCache: DecodedTextureCache;
  private categoryMaskCache: CategoryMaskCache;
  private matAlbedoCache: MatAlbedoCache;
  private mgnTextureCache: MgnTextureCache;

  private showTexturesActive = false;
  private activeSchemeKey = 'main';
  private activeSkin: Skin | null = null;
  private skinTable = new Map<string, Skin>();
  private variantSwappedAssetIds = new Set<string>();

  private aoEnabled = true;
  private mrMapEnabled = false;
  // WG hull normal maps are intrinsically subtle (mean tilt 2-3°; see
  // tmp/detail_test/probe_normal_intensity.py). Default 2.0 doubles
  // the apparent perturbation so detail reads under diffuse-dominated
  // lighting. 1.0 = engine-faithful, 0.0 = flat (normal map disabled
  // visually). Live-updatable via setNormalScale.
  private normalScale = 2.0;

  private hooks: TextureManagerInit;

  // Path-B diagnostic counters (logged at end of updateCamoUniforms).
  private lastPathBLog = '';

  constructor(init: TextureManagerInit) {
    this.renderer = init.renderer;
    this.hooks = init;
    this.repoBaseUrl = new URL('/repo/', window.location.origin).toString();
    this.decodedCache = new DecodedTextureCache(this.renderer);
    this.categoryMaskCache = new CategoryMaskCache(this.renderer, this.repoBaseUrl);
    this.matAlbedoCache = new MatAlbedoCache(this.renderer, this.repoBaseUrl);
    this.mgnTextureCache = new MgnTextureCache(this.renderer, this.repoBaseUrl);
  }

  // ── Bind index ────────────────────────────────────────────────────

  /**
   * Register a mesh under the binding key (`hull:<matName>` or
   * `asset:<assetId>` / `asset:<assetId>:material:<matName>`). The
   * entry adopts whatever schemes are already bound under the key.
   */
  registerMesh(
    mesh: THREE.Mesh,
    key: string,
    isAccessoryEntry: boolean,
    placement?: ShipPlacement,
  ): void {
    const existing = this.schemesByKey.get(key);
    const stem = key.startsWith('asset:')
      ? key.slice('asset:'.length)
      : key.startsWith('hull:')
        ? key.slice('hull:'.length)
        : key;
    const category =
      isAccessoryEntry && placement
        ? classifyPlacementCategory(stem, placement.category, placement.subcategory)
        : classifyPartCategory(stem);
    const entry: TextureMeshEntry = {
      mesh,
      slotUrlsByScheme: existing ?? new Map<string, SlotUrls>(),
      untextured: mesh.material,
      textured: null,
      texturedByScheme: new Map(),
      maskTextureByScheme: new Map(),
      isAccessoryEntry,
      assetId: placement?.asset_id ?? null,
      category,
      key,
    };
    this.entries.push(entry);
    let bucket = this.entriesByKey.get(key);
    if (!bucket) {
      bucket = [];
      this.entriesByKey.set(key, bucket);
    }
    bucket.push(entry);
  }

  /**
   * Bind every scheme's SlotUrls under one key in a single call. We
   * share the same Map reference across every entry under the key so a
   * future re-bind (rare — ship swaps clearShip first) updates them all.
   */
  bindSchemes(key: string, schemes: Map<string, SlotUrls>): void {
    this.schemesByKey.set(key, schemes);
    const bucket = this.entriesByKey.get(key);
    if (!bucket) return;
    for (const e of bucket) e.slotUrlsByScheme = schemes;
  }

  /**
   * Register a hull mesh under `hull:<materialName>`. No-op when the
   * material has no name (transparent armor/hitbox primitives).
   */
  registerHullMesh(mesh: THREE.Mesh): void {
    const matName = !Array.isArray(mesh.material) ? mesh.material?.name : '';
    if (matName) this.registerMesh(mesh, `hull:${matName}`, false);
  }

  /**
   * Register an accessory mesh, picking between per-material and
   * asset-level binding keys. Per-material wins iff the manager already
   * has non-empty schemes bound for `asset:<id>:material:<matName>` —
   * many library entries carry only the asset-level `texture_sets`, so
   * per-material would dead-end into a key with no schemes.
   */
  registerAccessoryMesh(mesh: THREE.Mesh, placement: ShipPlacement): void {
    const matName = !Array.isArray(mesh.material) ? mesh.material?.name : '';
    const perMatKey = matName ? `asset:${placement.asset_id}:material:${matName}` : '';
    const perMatBound = !!perMatKey && (this.schemesByKey.get(perMatKey)?.size ?? 0) > 0;
    const key = perMatBound ? perMatKey : `asset:${placement.asset_id}`;
    this.registerMesh(mesh, key, true, placement);
  }

  /**
   * Convenience: walk the sidecar's `materials[]` and bind each
   * per-material scheme under `hull:<material_id>`. Resolves DDS paths
   * against the hull GLB's directory (so per-stem `_camo_NN.dds` work).
   */
  bindHullMaterials(sidecar: SidecarDoc, hullBaseUrl: string): void {
    // variant_swapped_asset_ids covers swap-targets and bespoke attached
    // children of swapped parents — those ship bespoke `*_Azur_a.dds`-style
    // albedos that encode the variant's appearance, so any base-skin
    // overlay would double-paint. camo_skip_asset_ids (WG's engine-side
    // "_9" material-id denylist for whale / bage / Hoshino themed
    // geometry) is NOT unioned in here: the shader now honors
    // metallicGlossMap.B per-texel, and artists author mg.B=0 on those
    // parts so paint is suppressed at the texel level without a
    // mesh-level denylist. The producer still emits camo_skip_asset_ids
    // for schema stability; visual validation 2026-05-17 confirmed the
    // texel-level gate is sufficient.
    this.variantSwappedAssetIds = new Set(
      sidecar.ship?.variant_swapped_asset_ids ?? [],
    );
    for (const mat of sidecar.materials ?? []) {
      const matName = mat.material_id;
      if (!matName) continue;
      const schemes = this.compileSchemes(mat.texture_sets, hullBaseUrl);
      if (schemes.size > 0) this.bindSchemes(`hull:${matName}`, schemes);
      if (mat.shader_intent === 'transparent') this.markNoCamoKey(`hull:${matName}`);
      if (mat.shader_intent === 'cutout') this.cutoutKeys.add(`hull:${matName}`);
      const detail = (mat as { detail_params?: DetailParams }).detail_params;
      if (detail) this.detailParamsByKey.set(`hull:${matName}`, detail);
    }
  }

  /**
   * Mark a binding key as no-camo (sidecar `shader_intent: "transparent"`).
   * Order-independent — the dispatch + material-build paths read the
   * Set directly via `entry.key`, so bind-before-register and
   * register-before-bind both work without a retroactive flip.
   */
  private markNoCamoKey(key: string): void {
    this.noCamoKeys.add(key);
  }

  /**
   * Bind library-asset texture sets for a single asset_id. Called once
   * per unique asset during ShipViewer.loadShip's worker pool.
   *
   * Two-tier binding: per-material first (winning when the mesh's
   * material name matches), then asset-level fallback. Per-skin
   * `asset_overrides` are folded into the asset-level scheme map under
   * each skin's `scheme_key`.
   */
  bindLibraryAsset(
    assetId: string,
    libEntry: {
      glb: string;
      texture_sets?: Record<string, TextureSet>;
      materials?: Array<{
        material_id?: string;
        texture_sets?: Record<string, Record<string, { dds_mips: string[] }>>;
        [key: string]: unknown;
      }>;
    },
    sidecar: SidecarDoc | null,
    hullBaseUrl: string,
    /**
     * Sub-directory under `<workspace>/libraries/` where this asset
     * lives. Defaults to `'accessories'` for backwards compatibility
     * with every existing caller; the Projectiles route overrides
     * with `'projectiles'` so DDS paths in texture_sets resolve
     * against `/repo/libraries/projectiles/...` instead.
     */
    libraryRoot: string = 'accessories',
  ): void {
    const tplUrl = `/repo/libraries/${encodeURIComponent(libraryRoot)}/${libEntry.glb
      .split(/[\\/]/)
      .map(encodeURIComponent)
      .join('/')}`;
    const tplBase = new URL(tplUrl, window.location.origin).toString();

    // Per-material binding.
    for (const mat of libEntry.materials ?? []) {
      const matName = mat.material_id;
      if (!matName) continue;
      const schemes = this.compileSchemes(mat.texture_sets, tplBase);
      const matKey = `asset:${assetId}:material:${matName}`;
      if (schemes.size > 0) this.bindSchemes(matKey, schemes);
      const intent = (mat as { shader_intent?: string }).shader_intent;
      if (intent === 'transparent') this.markNoCamoKey(matKey);
      if (intent === 'cutout') this.cutoutKeys.add(matKey);
      const detail = (mat as { detail_params?: DetailParams }).detail_params;
      if (detail) this.detailParamsByKey.set(matKey, detail);
    }

    // Asset-level fallback. Used when per-material is empty (legacy
    // entries) or a mesh's material name doesn't match anything in
    // `materials[]`.
    const accessorySchemes = new Map<string, SlotUrls>();
    for (const [schemeKey, set] of Object.entries(libEntry.texture_sets ?? {})) {
      if (!set) continue;
      const slotUrls: SlotUrls = {};
      for (const slot of SLOTS) {
        const urls = resolveDdsMipUrls(set[slot], tplBase);
        if (urls.length > 0) slotUrls[slot] = urls;
      }
      if (Object.keys(slotUrls).length > 0) accessorySchemes.set(schemeKey, slotUrls);
    }

    // Per-skin asset overrides (v3.2 skin packs). Paths resolve relative
    // to the SHIP's hull GLB dir (not the library).
    for (const skin of sidecar?.skins ?? []) {
      const ovr = skin.asset_overrides?.[assetId];
      if (!ovr || !ovr.texture_sets) continue;
      const mainSet = ovr.texture_sets.main;
      if (!mainSet) continue;
      const slotUrls: SlotUrls = {};
      for (const slot of SLOTS) {
        const entry = mainSet[slot];
        if (entry && Array.isArray(entry.dds_mips)) {
          const urls = resolveDdsMipUrls(entry.dds_mips, hullBaseUrl);
          if (urls.length > 0) slotUrls[slot] = urls;
        }
      }
      if (Object.keys(slotUrls).length > 0) accessorySchemes.set(skin.scheme_key, slotUrls);
    }

    if (accessorySchemes.size > 0) this.bindSchemes(`asset:${assetId}`, accessorySchemes);
  }

  // Compile a `texture_sets` object (sidecar / library entry shape) into
  // a Map<schemeKey, SlotUrls> ready for `bindSchemes`. Empty schemes
  // omitted.
  private compileSchemes(
    tsByScheme: Record<string, SidecarTextureScheme> | undefined,
    baseUrl: string,
  ): Map<string, SlotUrls> {
    const out = new Map<string, SlotUrls>();
    for (const [schemeKey, scheme] of Object.entries(tsByScheme ?? {})) {
      if (!scheme) continue;
      const slotUrls: SlotUrls = {};
      for (const slot of SLOTS) {
        const mips = (scheme as Record<string, { dds_mips?: string[] } | undefined>)[slot]
          ?.dds_mips;
        const urls = resolveDdsMipUrls(mips, baseUrl);
        if (urls.length > 0) slotUrls[slot] = urls;
      }
      if (Object.keys(slotUrls).length > 0) out.set(schemeKey, slotUrls);
    }
    return out;
  }

  // ── Skin / toggle state ───────────────────────────────────────────

  setSkinTable(skins: Skin[]): void {
    this.skinTable.clear();
    for (const s of skins) this.skinTable.set(s.skin_id, s);
    // Auto-select the default skin so its overlay (categories +
    // mat_textures + color_scheme — populated for variant ships like
    // Baltimore_AzurLane via `_fold_variant_overlay_into_default`)
    // applies on first render.
    const def = this.skinTable.get('default') ?? skins[0] ?? null;
    this.activeSkin = def;
    this.activeSchemeKey = def?.scheme_key ?? 'main';
  }

  getSkins(): readonly Skin[] {
    return Array.from(this.skinTable.values());
  }

  getActiveSkinId(): string | null {
    return this.activeSkin?.skin_id ?? null;
  }

  isShowingTextures(): boolean {
    return this.showTexturesActive;
  }

  getAoEnabled(): boolean {
    return this.aoEnabled;
  }

  getMrMapEnabled(): boolean {
    return this.mrMapEnabled;
  }

  /**
   * Snapshot of the camo binding + per-entry state for the active skin.
   * Used by the ship inspector's debug panel. Counts walk the entries
   * lazily — no caching, so call once per refresh / revision bump.
   *
   * `painted = matAlbedo OR camo` per entry, mirroring the shader's
   * "any paint applied" condition (`gate1 || gate2` in shader.ts).
   */
  getCamoDiagnostics(): CamoDiagnostics {
    const skin = this.activeSkin;
    const cats = skin?.categories ?? {};
    const matTex = skin?.mat_textures ?? {};
    const palette = skin?.color_scheme?.colors ?? null;

    const categoryKeys = new Set<string>([
      ...Object.keys(cats),
      ...Object.keys(matTex),
    ]);
    const categories: CamoDiagnostics['categories'] = {};
    for (const cat of categoryKeys) {
      const c = cats[cat];
      const m = matTex[cat];
      categories[cat] = {
        hasMask: !!c?.mask,
        hasMgn: !!(c?.mgn ?? m?.mgn),
        hasMatAlbedo: !!m?.albedo,
        uvScale: (c?.uv?.scale ?? m?.uv?.scale) as [number, number] | undefined,
        uvOffset: (c?.uv?.offset ?? m?.uv?.offset) as [number, number] | undefined,
      };
    }

    let hullEntries = 0;
    let accessoryEntries = 0;
    let camoEnabled = 0;
    let matAlbedoEnabled = 0;
    let bothDisabled = 0;
    let noCamoEntries = 0;
    const perCategory: Record<string, { total: number; camoOn: number; matOn: number }> = {};

    for (const e of this.entries) {
      if (e.isAccessoryEntry) accessoryEntries++;
      else hullEntries++;
      if (this.noCamoKeys.has(e.key)) noCamoEntries++;

      let camoOn = 0;
      let matOn = 0;
      const clones: THREE.Material[] = [];
      if (e.textured) {
        if (Array.isArray(e.textured)) clones.push(...e.textured);
        else clones.push(e.textured);
      }
      for (const m of e.texturedByScheme.values()) {
        const arr = Array.isArray(m) ? m : [m];
        for (const mm of arr) if (!clones.includes(mm)) clones.push(mm);
      }
      for (const m of clones) {
        const u = (m.userData as { camoUniforms?: { camoEnable: { value: number }; matAlbedoEnable: { value: number } } }).camoUniforms;
        if (!u) continue;
        if (u.camoEnable.value > 0.5) camoOn = 1;
        if (u.matAlbedoEnable.value > 0.5) matOn = 1;
        break;
      }
      camoEnabled += camoOn;
      matAlbedoEnabled += matOn;
      if (camoOn === 0 && matOn === 0 && clones.length > 0) bothDisabled++;

      const cat = e.category;
      const bucket = perCategory[cat] ?? { total: 0, camoOn: 0, matOn: 0 };
      bucket.total++;
      bucket.camoOn += camoOn;
      bucket.matOn += matOn;
      perCategory[cat] = bucket;
    }

    return {
      activeSkinId: skin?.skin_id ?? null,
      schemeKey: this.activeSchemeKey,
      paletteColors: palette,
      categories,
      entryStats: {
        total: this.entries.length,
        hullEntries,
        accessoryEntries,
        camoEnabled,
        matAlbedoEnabled,
        bothDisabled,
        noCamoEntries,
      },
      perCategory,
      noCamoKeys: Array.from(this.noCamoKeys).sort(),
    };
  }

  // ── Runtime toggles ───────────────────────────────────────────────

  async setShowTextures(on: boolean, onProgress?: (msg: string) => void): Promise<void> {
    this.showTexturesActive = on;
    if (!on) {
      for (const e of this.entries) {
        e.mesh.material = e.untextured;
        if (e.isAccessoryEntry) this.hooks.onAccessoryMaterialSwap(e.mesh, e.untextured);
      }
      this.hooks.onAfterTextureApply();
      return;
    }
    await this.applyTextureState(this.activeSchemeKey, onProgress);
    await this.categoryMaskCache.ensureForSkin(this.activeSkin);
    await this.matAlbedoCache.ensureForSkin(this.activeSkin);
    await this.mgnTextureCache.ensureForSkin(this.activeSkin);
    this.updateCamoUniforms(this.activeSkin);
    onProgress?.(`Textures: ${this.decodedCache.size} DDS files decoded`);
  }

  /**
   * Pick a scheme key without going through the skin table. Used by the
   * library viewer's dead-variant toggle — there's no per-skin overlay to
   * apply, just a different texture set in `texture_sets.<key>`. No-op for
   * the ship page (which routes through `setActiveSkin`).
   *
   * Effect deferred to the next `setShowTextures` / `applyTextureState`;
   * call before flipping textures on.
   */
  setActiveSchemeKey(key: string): void {
    this.activeSchemeKey = key;
  }

  async setActiveSkin(skinId: string, onProgress?: (msg: string) => void): Promise<void> {
    const skin = this.skinTable.get(skinId) ?? null;
    const schemeKey = skin?.scheme_key ?? 'main';
    const schemeChanged = schemeKey !== this.activeSchemeKey;
    this.activeSchemeKey = schemeKey;
    this.activeSkin = skin;
    if (!this.showTexturesActive) return;
    if (schemeChanged) await this.applyTextureState(schemeKey, onProgress);
    await this.categoryMaskCache.ensureForSkin(this.activeSkin);
    await this.matAlbedoCache.ensureForSkin(this.activeSkin);
    await this.mgnTextureCache.ensureForSkin(this.activeSkin);
    this.updateCamoUniforms(this.activeSkin);
    onProgress?.(`Active skin: ${skinId}`);
  }

  setAoEnabled(on: boolean): void {
    if (this.aoEnabled === on) return;
    this.aoEnabled = on;
    const intensity = on ? 1.0 : 0.0;
    for (const entry of this.entries) {
      if (!entry.textured) continue;
      for (const mat of asArray(entry.textured)) {
        const std = mat as THREE.MeshStandardMaterial;
        if ('isMeshStandardMaterial' in std && std.isMeshStandardMaterial) {
          std.aoMapIntensity = intensity;
        }
      }
    }
  }

  setMrMapEnabled(on: boolean): void {
    if (this.mrMapEnabled === on) return;
    this.mrMapEnabled = on;
    for (const entry of this.entries) {
      if (!entry.textured) continue;
      for (const mat of asArray(entry.textured)) {
        const std = mat as THREE.MeshStandardMaterial;
        if (!('isMeshStandardMaterial' in std) || !std.isMeshStandardMaterial) continue;
        const ud = std.userData ?? {};
        const mgTex = (ud.mgTex ?? null) as THREE.Texture | null;
        if (!mgTex) continue;
        std.metalnessMap = on ? mgTex : null;
        std.roughnessMap = on ? mgTex : null;
        if (on) {
          std.metalness = (ud.origMetalness ?? std.metalness) as number;
          std.roughness = (ud.origRoughness ?? std.roughness) as number;
        } else {
          std.metalness = 0.0;
          std.roughness = 0.8;
        }
        std.needsUpdate = true;
      }
    }
  }

  /**
   * Update the global normal-map intensity. Walks every textured clone
   * and rewrites `MeshStandardMaterial.normalScale`. Clones built later
   * pick up the new value via `clonePolicy()`.
   *
   * Default 2.0 — WG art convention is very gentle hull perturbation
   * (see `tmp/detail_test/probe_normal_intensity.py` for tilt-angle
   * distributions). 1.0 = engine-faithful; values up to ~4 stay
   * visually plausible.
   */
  setNormalScale(value: number): void {
    if (this.normalScale === value) return;
    this.normalScale = value;
    for (const entry of this.entries) {
      if (!entry.textured) continue;
      for (const mat of asArray(entry.textured)) {
        const std = mat as THREE.MeshStandardMaterial;
        if (!('isMeshStandardMaterial' in std) || !std.isMeshStandardMaterial) continue;
        if (!std.normalMap) continue;
        std.normalScale.set(value, value);
      }
      // Per-scheme clones too (skin-pack overrides etc.).
      for (const mat of entry.texturedByScheme.values()) {
        for (const mm of asArray(mat)) {
          const std = mm as THREE.MeshStandardMaterial;
          if (!('isMeshStandardMaterial' in std) || !std.isMeshStandardMaterial) continue;
          if (!std.normalMap) continue;
          std.normalScale.set(value, value);
        }
      }
    }
  }

  getNormalScale(): number {
    return this.normalScale;
  }

  /**
   * Snapshot of normal-map binding state, for runtime diagnostics
   * (live-inspect via `viewer.getNormalDiagnostics()` in dev console).
   * Useful when the slider appears to have no effect — surfaces
   * whether materials actually have `normalMap` bound and what scale
   * is live on them.
   */
  getNormalDiagnostics(): {
    normalScale: number;
    totalEntries: number;
    texturedEntries: number;
    withNormalMap: number;
    withoutNormalMap: number;
    sample: {
      key: string;
      hasNormalMap: boolean;
      normalScale: [number, number];
      wgPackN: boolean;
    } | null;
  } {
    let texturedEntries = 0;
    let withNormalMap = 0;
    let withoutNormalMap = 0;
    let sample: ReturnType<TextureManager['getNormalDiagnostics']>['sample'] = null;

    for (const entry of this.entries) {
      if (!entry.textured) continue;
      texturedEntries++;
      const mats = asArray(entry.textured);
      for (const mat of mats) {
        const std = mat as THREE.MeshStandardMaterial;
        if (!('isMeshStandardMaterial' in std) || !std.isMeshStandardMaterial) continue;
        if (std.normalMap) withNormalMap++; else withoutNormalMap++;
        if (sample === null && std.normalMap) {
          sample = {
            key: entry.key,
            hasNormalMap: true,
            normalScale: [std.normalScale.x, std.normalScale.y],
            wgPackN: !!(std.normalMap.userData?.wgPackN),
          };
        }
      }
    }

    return {
      normalScale: this.normalScale,
      totalEntries: this.entries.length,
      texturedEntries,
      withNormalMap,
      withoutNormalMap,
      sample,
    };
  }

  // ── Lifecycle ─────────────────────────────────────────────────────

  /** Per-ship cleanup. Drops entries + scheme bindings; KEEPS the decoded
   *  texture cache (most ship swaps reuse the same library textures). */
  clearShip(): void {
    this.entries.length = 0;
    this.schemesByKey.clear();
    this.entriesByKey.clear();
    this.noCamoKeys.clear();
    this.cutoutKeys.clear();
    this.detailParamsByKey.clear();
    this.categoryMaskCache.clear();
    this.matAlbedoCache.clear();
    this.mgnTextureCache.clear();
    this.showTexturesActive = false;
    this.activeSchemeKey = 'main';
    this.activeSkin = null;
    this.skinTable.clear();
    this.variantSwappedAssetIds.clear();
  }

  /** Terminal cleanup — also drop the decoded texture cache + dummies. */
  dispose(): void {
    this.clearShip();
    this.decodedCache.clear();
    dummyMaskTexture.dispose();
    dummyMatAlbedoTexture.dispose();
  }

  // ── Internals ─────────────────────────────────────────────────────

  private clonePolicy(): MaterialClonePolicy {
    return {
      aoEnabled: this.aoEnabled,
      mgMapEnabled: this.mrMapEnabled,
      normalScale: this.normalScale,
    };
  }

  private async resolveTexturesForEntry(
    entry: TextureMeshEntry,
    schemeKey: string,
  ): Promise<TextureSetResolved> {
    const active = entry.slotUrlsByScheme.get(schemeKey) ?? {};
    const main = entry.slotUrlsByScheme.get('main') ?? {};
    const out: TextureSetResolved = {};
    await Promise.all(
      SLOTS.map(async (slot) => {
        const urls = active[slot] ?? main[slot];
        if (!urls || urls.length === 0) return;
        const tex = await this.decodedCache.decode(slot, urls);
        if (tex) out[slot] = tex;
      }),
    );
    return out;
  }

  // Decode + cache the camo mask for a given entry's active scheme.
  // No-op for `main` (which has no mask — main = base albedo).
  private async ensureMaskTexture(
    entry: TextureMeshEntry,
    schemeKey: string,
  ): Promise<THREE.Texture | null> {
    if (schemeKey === 'main') return null;
    const cached = entry.maskTextureByScheme.get(schemeKey);
    if (cached) return cached;
    const urls = entry.slotUrlsByScheme.get(schemeKey)?.baseColor;
    if (!urls || urls.length === 0) return null;
    const tex = await this.decodedCache.decode('baseColor', urls);
    if (!tex) return null;
    entry.maskTextureByScheme.set(schemeKey, tex);
    return tex;
  }

  private async applyTextureState(
    schemeKey: string,
    onProgress?: (msg: string) => void,
  ): Promise<void> {
    const queue = this.entries.slice();
    let done = 0;
    const policy = this.clonePolicy();

    const worker = async () => {
      while (queue.length > 0) {
        const e = queue.shift();
        if (!e) return;
        if (e.slotUrlsByScheme.size === 0) {
          done++;
          continue;
        }

        const hasOwnBase =
          e.slotUrlsByScheme.has(schemeKey) &&
          (e.slotUrlsByScheme.get(schemeKey)!.baseColor?.length ?? 0) > 0;
        const cloneKey = hasOwnBase ? schemeKey : 'main';

        let textured = e.texturedByScheme.get(cloneKey) ?? null;
        if (!textured && cloneKey === 'main') textured = e.textured;

        if (!textured) {
          const tex = await this.resolveTexturesForEntry(e, cloneKey);
          if (Object.keys(tex).length === 0) {
            done++;
            continue;
          }
          const detailParams = this.detailParamsByKey.get(e.key) ?? null;
          textured = buildTextured(
            e.untextured,
            tex,
            policy,
            this.noCamoKeys.has(e.key),
            detailParams,
            this.cutoutKeys.has(e.key),
          );
          e.texturedByScheme.set(cloneKey, textured);
        } else {
          e.texturedByScheme.set(cloneKey, textured);
        }
        if (cloneKey === 'main') e.textured = textured;

        await this.ensureMaskTexture(e, schemeKey);

        if (!this.showTexturesActive || this.activeSchemeKey !== schemeKey) return;
        e.mesh.material = textured;
        if (e.isAccessoryEntry) this.hooks.onAccessoryMaterialSwap(e.mesh, textured);
        done++;
        if ((done & 0x1f) === 0 || done === this.entries.length) {
          onProgress?.(`Textures ${done}/${this.entries.length}…`);
        }
      }
    };

    await Promise.all(Array.from({ length: PARALLEL }, () => worker()));
    this.hooks.onAfterTextureApply();
  }

  /**
   * Push the active skin's palette + mask into every entry's textured
   * clone. See `tools/webview/src/ship.ts` legacy comment block above
   * `updateCamoUniforms` for the full per-entry priority + the
   * Path A / Path B prefer-mgn dispatch.
   */
  private updateCamoUniforms(skin: Skin | null): void {
    const palette = skin?.color_scheme ?? null;
    const categories = skin?.categories ?? null;
    const matTextures = skin?.mat_textures ?? null;

    let pathBCount = 0;
    let pathBSuppressedMasks = 0;
    const pathBCats = new Set<string>();
    if (categories) {
      for (const [catKey, cat] of Object.entries(categories)) {
        if (cat.mgn || cat.params || cat.anim_map) {
          pathBCount++;
          pathBCats.add(catKey);
          if (cat.mask) pathBSuppressedMasks++;
        }
      }
    }
    // mat_textures-side MGN is the other Path B emit path (mat_palette
    // hybrid skins like mat_Montana_Hoshino — accessories painted with
    // mat_camo + per-part `<*_mgn>` for gloss/metallic/normal modulation).
    if (matTextures) {
      for (const [catKey, matCat] of Object.entries(matTextures)) {
        if (matCat.mgn || matCat.anim_map) {
          pathBCount++;
          pathBCats.add(catKey);
        }
      }
    }

    for (const entry of this.entries) {
      // Sidecar-transparent materials get the camo chunk attached at
      // material-build time (no early skip — see material.ts), but the
      // dispatch must not enable camo on them. Forcing mask + matTex +
      // pathB to null below routes their uniform push down the
      // all-disabled branch, matching the pre-2026-05-17 behaviour
      // where the chunk wasn't attached at all.
      const noCamo = this.noCamoKeys.has(entry.key);

      let mask: THREE.Texture | null = null;
      let uvScaleX = 1.0,
        uvScaleY = 1.0,
        uvOffsetX = 0.0,
        uvOffsetY = 0.0;

      // Variant-swapped accessories opt out of overlays so their bespoke
      // `*_Azur_a.dds` albedo wins over generic camo / mat_camo.
      const variantOptOut =
        entry.assetId !== null && this.variantSwappedAssetIds.has(entry.assetId);

      // Path B MGN data — both the categories block (Path B-only emit)
      // and the mat_textures block (mat_palette hybrid emit) can surface
      // `mgn` + `params`. Per CAMO_SOURCE_OF_TRUTH §4.9, `mat_textures`
      // wins over `categories` when both exist; the two record shapes
      // diverge in their non-MGN fields, but both carry `.mgn` + `.params`
      // identically, so the downstream uniform writes are the same.
      const pathB: { mgn?: { dds_mips: string[] }; params?: SkinMatCategoryParams } | null =
        noCamo
          ? null
          : matTextures && entry.category in matTextures && matTextures[entry.category].mgn
            ? matTextures[entry.category]
            : categories && entry.category in categories && categories[entry.category].mgn
              ? categories[entry.category]
              : null;
      let mgnTex: THREE.Texture | null = null;
      let mgnInfluence: [number, number, number] = [0, 0, 0];
      let useCamoMaskGlobal = false;
      if (pathB && !variantOptOut) {
        mgnTex = this.mgnTextureCache.get(pathB.mgn!);
        if (pathB.params) {
          // mgn_influence schema is a 4-tuple but the .w slot is a dead
          // pad (DXBC-confirmed; see camo_path_b_makecamomaterial_re.md §1).
          // Three.Vector3.set takes 3 args.
          const mi = pathB.params.mgn_influence;
          mgnInfluence = [mi[0], mi[1], mi[2]];
          useCamoMaskGlobal = pathB.params.use_camo_mask_global;
        }
      }

      if (!noCamo && palette && categories && !variantOptOut && entry.category in categories) {
        // Step 1: category override. Engine per-part rule: when
        // <*_mgn> exists for the part, Path B wins and Path A mask is
        // ignored. We mirror that by skipping `mask` binding when
        // `cat.mgn` is set, even if `cat.mask` is also present (~17%
        // of corpus). The MGN texture + channel-mix is bound via
        // `mgnTex` / `mgnInfluence` above and consumed in the shader's
        // roughness / metalness / normal chunks.
        const cat = categories[entry.category];
        if (!cat.mgn && cat.mask) {
          mask = this.categoryMaskCache.get(cat.mask);
          uvScaleX = cat.uv.scale[0];
          uvScaleY = cat.uv.scale[1];
          uvOffsetX = cat.uv.offset[0];
          uvOffsetY = cat.uv.offset[1];
        }
      } else if (!noCamo && palette) {
        // Step 2: per-stem `_camo_NN.dds` cascade.
        mask = entry.maskTextureByScheme.get(this.activeSchemeKey) ?? null;
      }

      let matTex: THREE.Texture | null = null;
      let matUx = 1.0,
        matUy = 1.0,
        matOx = 0.0,
        matOy = 0.0;
      let matMode = -1.0;
      let matAo = 0.0;
      if (!noCamo && matTextures && entry.category in matTextures) {
        const matCat = matTextures[entry.category];
        const params = matCat.params ?? null;
        // mat_albedo overlay applied regardless of variantOptOut.
        // Engine "_9" themed exclusions (whale, bage, Hoshino) and
        // variant-swap bespoke albedos both author mg.B=0, so the
        // shader's per-texel paintMask gate suppresses paint naturally
        // without a consumer-side opt-out. Visual validation 2026-05-17.
        if (params && params.camo_mode === 0) {
          // Path B explicitly disabled — leave matTex null.
        } else {
          matTex = this.matAlbedoCache.get(matCat.albedo);
          matUx = matCat.uv.scale[0];
          matUy = matCat.uv.scale[1];
          matOx = matCat.uv.offset[0];
          matOy = matCat.uv.offset[1];
          if (params) {
            matMode = params.camo_mode;
            matAo = params.ao_influence;
          }
        }
      }

      const allClones: THREE.Material[] = [];
      if (entry.textured) for (const m of asArray(entry.textured)) allClones.push(m);
      for (const m of entry.texturedByScheme.values()) {
        for (const mm of asArray(m)) if (!allClones.includes(mm)) allClones.push(mm);
      }

      for (const mat of allClones) {
        for (const u of uniformsOf(mat)) {
          if (matTex) {
            u.matAlbedoEnable.value = 1.0;
            u.matAlbedoMap.value = matTex;
            u.matAlbedoUv.value.set(matUx, matUy, matOx, matOy);
            u.matAlbedoMode.value = matMode;
            u.matAlbedoAo.value = matAo;
            u.camoEnable.value = 0.0;
            u.maskMap.value = dummyMaskTexture;
            u.camoUV.value.set(1, 1, 0, 0);
          } else if (mask && palette) {
            u.matAlbedoEnable.value = 0.0;
            u.matAlbedoMap.value = dummyMatAlbedoTexture;
            u.matAlbedoUv.value.set(1, 1, 0, 0);
            u.matAlbedoMode.value = -1.0;
            u.matAlbedoAo.value = 0.0;
            u.camoEnable.value = 1.0;
            u.maskMap.value = mask;
            u.camoUV.value.set(uvScaleX, uvScaleY, uvOffsetX, uvOffsetY);
            for (let i = 0; i < 4; i++) {
              const c = palette.colors[i] ?? [0, 0, 0, 1];
              u.camoColors.value[i].set(c[0], c[1], c[2], c[3]);
            }
          } else {
            u.matAlbedoEnable.value = 0.0;
            u.matAlbedoMap.value = dummyMatAlbedoTexture;
            u.matAlbedoUv.value.set(1, 1, 0, 0);
            u.matAlbedoMode.value = -1.0;
            u.matAlbedoAo.value = 0.0;
            u.camoEnable.value = 0.0;
            u.maskMap.value = dummyMaskTexture;
            u.camoUV.value.set(1, 1, 0, 0);
          }

          // Path B MGN override — bound independently of the matAlbedo
          // (Path A color) branch. The shader gates each per-channel
          // override on `catMgnBound * Influence_<chan>` so a zero
          // influence is a no-op. When the sidecar carries an `mgn`
          // texture but the cache didn't load it (missing DDS, etc.),
          // mgnTex is null → catMgnBound = 0 → safe fallback to base.
          if (mgnTex) {
            u.catMgnMap.value = mgnTex;
            u.catMgnBound.value = 1.0;
            u.catMgnInfluence.value.set(mgnInfluence[0], mgnInfluence[1], mgnInfluence[2]);
          } else {
            u.catMgnBound.value = 0.0;
            u.catMgnInfluence.value.set(0, 0, 0);
          }

          // mg.B paint gate. Engine ties `useCamoMaskGlobal` to Path B's
          // <*_mgn> params, but the flag's job is to fold the per-pixel
          // mg.B exclusion into the gate that drives BOTH paint and MGN
          // overrides. Three sources need it ON:
          //   • MGN-bound with params.use_camo_mask_global=true (engine
          //     Path B proper) — preserved as before.
          //   • mat_albedo bound without MGN (mat_palette hybrid skins
          //     like mat_Montana_Hoshino — variant tile paint over
          //     accessories) — needs ON so artist-authored mg.B=0
          //     regions stay unpainted. Without this, accessory normals
          //     rarely carry the _n.B 4-threshold deny pattern → nbPaint
          //     defaults to 1 → catPaintMask collapses to 1 → paint
          //     applies across the entire mesh including exclusion zones.
          //   • Path A camoEnable path doesn't use catPaintMask at all
          //     (it gates by `mgB` directly), so leaving the flag set
          //     here is a no-op for that branch.
          const useGate = (mgnTex && useCamoMaskGlobal) || !!matTex;
          u.catUseCamoMaskGlobal.value = useGate ? 1.0 : 0.0;
        }
      }
    }

    if (pathBCount > 0) {
      const catList = Array.from(pathBCats);
      const msg =
        `[ship.camo] skin=${skin?.skin_id ?? '?'} Path B on ` +
        `${catList.length} categor${catList.length === 1 ? 'y' : 'ies'}: ` +
        `[${catList.join(', ')}]; ${pathBSuppressedMasks} hybrid mask(s) suppressed. ` +
        'MGN channel override RENDERED (gloss / metallic / normal per Influence_*).';
      if (msg !== this.lastPathBLog) {
        console.log(msg);
        this.lastPathBLog = msg;
      }
    }
  }
}

function asArray<T>(x: T | T[]): T[] {
  return Array.isArray(x) ? x : [x];
}

// Re-export for ergonomic imports.
export { applyTexturesToMaterial };
