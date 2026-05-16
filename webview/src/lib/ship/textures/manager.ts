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
//   • setAoEnabled / setMrMapEnabled / setPreserveUnderwaterHull — diagnostics
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
import type { ShipPlacement, Skin, SidecarDoc, SidecarTextureScheme, TextureSet } from '$lib/types';
import { dummyMaskTexture, dummyMatAlbedoTexture, uniformsOf } from '../camo';
import { applyTexturesToMaterial, buildTextured, type MaterialClonePolicy } from './material';
import { CategoryMaskCache, MatAlbedoCache } from './category_mask';
import { DecodedTextureCache } from './decode';
import type { SlotUrls, TextureMeshEntry, TextureSetResolved } from './types';

const SLOTS: (keyof TextureSet)[] = [
  'baseColor',
  'metallicRoughness',
  'normal',
  'occlusion',
  'emissive',
  'camoMask',
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

  private decodedCache: DecodedTextureCache;
  private categoryMaskCache: CategoryMaskCache;
  private matAlbedoCache: MatAlbedoCache;

  private showTexturesActive = false;
  private activeSchemeKey = 'main';
  private activeSkin: Skin | null = null;
  private skinTable = new Map<string, Skin>();
  private variantSwappedAssetIds = new Set<string>();

  private aoEnabled = true;
  private mrMapEnabled = false;
  private preserveUnderwater = true;

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
    // Union both opt-out sets: variant_swapped_asset_ids covers swap-targets
    // and bespoke attached children of swapped parents; camo_skip_asset_ids
    // covers WG's engine-side per-mesh "_9" material-id marker (themed /
    // skin-exclusive decorative geometry like AM6067_Whale_Hoshino, Azur
    // Lane secondaries, Ayane gun barrels). Both classes of asset should
    // bypass camo painting — the runtime engine never enters camo
    // dispatch for "_9" materials (verified via Ghidra on the static
    // material-name → part_index table at exe 0x140071a20).
    this.variantSwappedAssetIds = new Set([
      ...(sidecar.ship?.variant_swapped_asset_ids ?? []),
      ...(sidecar.ship?.camo_skip_asset_ids ?? []),
    ]);
    for (const mat of sidecar.materials ?? []) {
      const matName = mat.material_id;
      if (!matName) continue;
      const schemes = this.compileSchemes(mat.texture_sets, hullBaseUrl);
      if (schemes.size > 0) this.bindSchemes(`hull:${matName}`, schemes);
    }
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
  ): void {
    const tplUrl = `/repo/libraries/accessories/${libEntry.glb
      .split(/[\\/]/)
      .map(encodeURIComponent)
      .join('/')}`;
    const tplBase = new URL(tplUrl, window.location.origin).toString();

    // Per-material binding.
    for (const mat of libEntry.materials ?? []) {
      const matName = mat.material_id;
      if (!matName) continue;
      const schemes = this.compileSchemes(mat.texture_sets, tplBase);
      if (schemes.size > 0) this.bindSchemes(`asset:${assetId}:material:${matName}`, schemes);
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

  getPreserveUnderwater(): boolean {
    return this.preserveUnderwater;
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

  setPreserveUnderwaterHull(on: boolean): void {
    if (this.preserveUnderwater === on) return;
    this.preserveUnderwater = on;
    const value = on ? 0.0 : -1e9;
    for (const entry of this.entries) {
      if (!entry.textured) continue;
      for (const mat of asArray(entry.textured)) {
        for (const u of uniformsOf(mat)) u.waterlineY.value = value;
      }
    }
  }

  // ── Lifecycle ─────────────────────────────────────────────────────

  /** Per-ship cleanup. Drops entries + scheme bindings; KEEPS the decoded
   *  texture cache (most ship swaps reuse the same library textures). */
  clearShip(): void {
    this.entries.length = 0;
    this.schemesByKey.clear();
    this.entriesByKey.clear();
    this.categoryMaskCache.clear();
    this.matAlbedoCache.clear();
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
      waterlineY: this.preserveUnderwater ? 0.0 : -1e9,
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
          textured = buildTextured(e.untextured, tex, policy);
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
    const pathBCats: string[] = [];
    if (categories) {
      for (const [catKey, cat] of Object.entries(categories)) {
        if (cat.mgn || cat.params || cat.anim_map) {
          pathBCount++;
          pathBCats.push(catKey);
          if (cat.mask) pathBSuppressedMasks++;
        }
      }
    }

    for (const entry of this.entries) {
      let mask: THREE.Texture | null = null;
      let uvScaleX = 1.0,
        uvScaleY = 1.0,
        uvOffsetX = 0.0,
        uvOffsetY = 0.0;

      // Variant-swapped accessories opt out of overlays so their bespoke
      // `*_Azur_a.dds` albedo wins over generic camo / mat_camo.
      const variantOptOut =
        entry.assetId !== null && this.variantSwappedAssetIds.has(entry.assetId);

      if (palette && categories && !variantOptOut && entry.category in categories) {
        // Step 1: category override. Engine per-part rule: when
        // <*_mgn> exists for the part, Path B wins and Path A mask is
        // ignored. We mirror that by skipping `mask` binding when
        // `cat.mgn` is set, even if `cat.mask` is also present (~17%
        // of corpus). Full Path B shader render (MGN channel override)
        // is TODO: bind mgn at a `catMgnMap` uniform + apply per-channel
        // mix.
        const cat = categories[entry.category];
        if (!cat.mgn && cat.mask) {
          mask = this.categoryMaskCache.get(cat.mask);
          uvScaleX = cat.uv.scale[0];
          uvScaleY = cat.uv.scale[1];
          uvOffsetX = cat.uv.offset[0];
          uvOffsetY = cat.uv.offset[1];
        }
      } else if (palette) {
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
      if (matTextures && !variantOptOut && entry.category in matTextures) {
        const matCat = matTextures[entry.category];
        const params = matCat.params ?? null;
        // Engine per-part rule (selector at +0x188+part*0xc0 in
        // makeCamoMaterial — RE'd at exe 0x14108ad80): when `<*_mgn>`
        // exists for the part, Path B wins over the Path A mat_camo
        // overlay. Mirrors the `categories`-block prefer-mgn dispatch
        // above. Skipping the binding lets the asset render with its
        // natural albedo — correct when params default camo_mode = -1
        // ("no override") which is what hybrid mat_palette skins carry
        // on their gun/director/misc/wire categories. Full Path B
        // render is TODO per project_camo_hybrid_path_ab.md.
        if (matCat.mgn) {
          // Path B configured — skip Path A.
        } else if (params && params.camo_mode === 0) {
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
        }
      }
    }

    if (pathBCount > 0) {
      const msg =
        `[ship.camo] skin=${skin?.skin_id ?? '?'} Path B on ` +
        `${pathBCount} categor${pathBCount === 1 ? 'y' : 'ies'}: ` +
        `[${pathBCats.join(', ')}]; ${pathBSuppressedMasks} hybrid mask(s) suppressed. ` +
        'MGN channel override not yet rendered (TODO).';
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
