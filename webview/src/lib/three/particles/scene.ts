// Public API: ParticleScene, one per ShipViewer instance.
// Extracted verbatim from the former monolithic particles.ts.

import * as THREE from 'three';
import type { ParticleAttachment, ParticleRecord } from '$lib/types/sidecar';
import { fetchParticleRecord, repoUrl } from '$lib/api';
import { loadDdsMipChain, loadDdsSoftwareRgbaTexture } from '$lib/dds';
import {
  CHILD_EFFECT_BUDGET,
  CHILD_EFFECT_DEPTH_LIMIT,
  DEFAULT_PARTICLE_LIFETIME,
  DEFAULT_PARTICLE_SUN_COLOR_NORM,
  DEFAULT_PARTICLE_SUN_DIR,
  PARTICLE_POINT_LIGHT_BUDGET,
} from './constants';
import {
  finiteNumber,
  hasNonZeroNumber,
  normalizedParticleSunColor,
  rampHasNonZeroValue,
  systemUsesDetachedCoordinateFrame,
} from './sampling';
import { SystemRenderer } from './system-renderer';
import type { ParticleEffectSpawnCallback, ParticleEffectSpawnRequest } from './system-renderer';
import { LightRenderer } from './light-renderer';
import { PS_RBT_LUT_MODES, buildParticleMaterial } from './material';

// ---------------------------------------------------------------------------
// Public API — one ParticleScene per ShipViewer instance
// ---------------------------------------------------------------------------

export interface ParticleAttachmentHandle {
  attachment: ParticleAttachment;
  group: THREE.Group;
  /** Per-system simulators inside the attachment. */
  systems: SystemRenderer[];
  /** Decoded kind=light components rendered as glow sprites, with the
   *  strongest subset also promoted to real point lights. */
  lights: LightRenderer[];
  /** The parsed source record this attachment renders. Carries the
   *  authoring data the UI inspector needs (renderer.textureName0,
   *  general.capacity, components[].action, …) without poking through
   *  SystemRenderer internals. */
  record: ParticleRecord;
  intensityValues?: number[];
  active: boolean;
}

interface SpawnedParticleEffect {
  parent: ParticleAttachmentHandle;
  group: THREE.Group;
  systems: SystemRenderer[];
  lights: LightRenderer[];
  depth: number;
}

type ParticleQuality = 'high' | 'low' | 'shared';

interface ParticleEffectRef {
  path: string;
  quality: ParticleQuality;
}

function normalizeParticleEffectPath(path: string): string {
  return parseParticleEffectRef(path).path;
}

function parseParticleEffectRef(path: string): ParticleEffectRef {
  let p = path.replace(/\\/g, '/').trim().replace(/^\/+/, '');
  if (p.startsWith('?')) p = p.slice(1);
  let quality: ParticleQuality = 'high';
  const suffix = p.slice(p.lastIndexOf('/') + 1);
  if (suffix === 'high' || suffix === 'low' || suffix === 'shared') {
    quality = suffix;
    p = p.slice(0, p.lastIndexOf('/'));
  }
  return { path: p, quality };
}

function particleRecordCacheKey(path: string, quality: ParticleQuality): string {
  return `${path}#${quality}`;
}

function intensityDefaultsForRecord(record: ParticleRecord): number[] {
  return (record.intensityChannels ?? []).map((channel) =>
    finiteNumber(channel.defaultIntensity, 1),
  );
}

/**
 * Manages the scene-level particle layer for one ship: a root group +
 * one sub-group per attachment. Created when the sidecar is loaded;
 * disposed when the ship is unloaded.
 *
 * Texture lifetime: DDS maps referenced by the particle systems are
 * loaded on demand and cached in `textureCache` keyed by absolute URL.
 * Two systems pointing at the same `Fire01.dds` share the THREE.Texture
 * instance and the cache disposes them all on `dispose()`.
 */
export class ParticleScene {
  readonly root: THREE.Group;
  private attachments = new Map<string, ParticleAttachmentHandle>();
  private lastTickMs = -1;
  private sunDirection = DEFAULT_PARTICLE_SUN_DIR.clone();
  private sunColorNorm = DEFAULT_PARTICLE_SUN_COLOR_NORM.clone();
  /** WebGL renderer used to issue DDS compressed-texture uploads.
   *  Provided once via `setRenderer`. Until set, particle systems load
   *  with the procedural-disc fallback. */
  private renderer: THREE.WebGLRenderer | null = null;
  /** Cache: absolute URL → in-flight or resolved THREE.Texture. Shared
   *  across emitters so duplicate `Fire01.dds` refs upload once. */
  private textureCache = new Map<string, Promise<THREE.Texture | null>>();
  private particleRecords = new Map<string, ParticleRecord>();
  private particleRecordFetches = new Map<string, Promise<ParticleRecord | null>>();
  private spawnedEffects: SpawnedParticleEffect[] = [];
  private sortCamera: THREE.Camera | null = null;
  private viewportSize = new THREE.Vector2();

  constructor(renderer?: THREE.WebGLRenderer) {
    this.root = new THREE.Group();
    this.root.name = 'ParticleEffects';
    if (renderer) this.renderer = renderer;
    // DEV ONLY: expose the live scene to the native↔webview particle parity
    // harness (tmp/pfx_re/route2/particle_webview_envelope.*). Reads per-slot
    // cooked state via debugAllSystems()[i].debugSnapshot().
    if ((import.meta as { env?: { DEV?: boolean } }).env?.DEV) {
      (globalThis as { __particleScene?: ParticleScene }).__particleScene = this;
    }
  }

  /** DEV ONLY: flat list of every live SystemRenderer (attachments + spawned
   *  effects), in record-system order so index i ↔ engine cfgIndex i. */
  debugAllSystems(): SystemRenderer[] {
    const out: SystemRenderer[] = [];
    for (const h of this.attachments.values()) out.push(...h.systems);
    for (const e of this.spawnedEffects) out.push(...e.systems);
    return out;
  }

  /** Provide the WebGL renderer used to upload DDS textures. Safe to
   *  call after construction (idempotent — subsequent calls update the
   *  reference but already-cached textures stay valid since DDS uploads
   *  bind to the GL context not a specific renderer instance). */
  setRenderer(renderer: THREE.WebGLRenderer): void {
    this.renderer = renderer;
  }

  /** Camera used for WG's order-dependent particle draw sorting. */
  setSortCamera(camera: THREE.Camera | null): void {
    this.sortCamera = camera;
    for (const handle of this.attachments.values()) {
      for (const system of handle.systems) system.setSortCamera(camera);
    }
    for (const effect of this.spawnedEffects) {
      for (const system of effect.systems) system.setSortCamera(camera);
    }
  }

  /** Keep particle lightmap reconstruction synced to the scene's key sun.
   *  `direction` points toward the sun, matching `createSceneEnvironment`.
   *  `color` is normalized with WG's colored Reinhard term. */
  setSunLighting(direction: THREE.Vector3, color: THREE.Color): void {
    if (direction.lengthSq() > 1e-10) {
      this.sunDirection.copy(direction).normalize();
    }
    this.sunColorNorm.copy(normalizedParticleSunColor(color));
    for (const handle of this.attachments.values()) {
      for (const system of handle.systems) this.applySunLighting(system.material);
    }
    for (const effect of this.spawnedEffects) {
      for (const system of effect.systems) this.applySunLighting(system.material);
    }
  }

  /** Build the scene from a sidecar's `effects` block. Returns the
   *  flat list of attachment handles for UI binding.
   *
   *  `options.loopOneShot` decides per attachment whether a one-shot effect
   *  loops for inspection (inspector/ambient default) or plays once and
   *  finishes (ship-view event effects — muzzle/explosion — whose lifetime is
   *  governed by the trigger, mirroring the native fire-once-then-kill
   *  EffectManager model). `restartAttachment()` re-fires a finished one. */
  build(
    attachments: ParticleAttachment[],
    particles: Record<string, ParticleRecord>,
    resolveNodePosition: (attachment: ParticleAttachment) => THREE.Vector3 | null,
    options: { loopOneShot?: (attachment: ParticleAttachment) => boolean } = {},
  ): ParticleAttachmentHandle[] {
    this.clear();
    this.particleRecords.clear();
    this.particleRecordFetches.clear();
    for (const [path, record] of Object.entries(particles)) {
      const effectRef = parseParticleEffectRef(path);
      this.particleRecords.set(particleRecordCacheKey(effectRef.path, effectRef.quality), record);
      if (effectRef.quality === 'high') this.particleRecords.set(effectRef.path, record);
    }
    const handles: ParticleAttachmentHandle[] = [];
    for (let i = 0; i < attachments.length; i++) {
      const a = attachments[i];
      const particlePath = normalizeParticleEffectPath(a.particle_path);
      const rec = particles[a.particle_path] ?? particles[particlePath];
      if (!rec) continue;
      const grp = new THREE.Group();
      grp.name = `effect:${a.group}:${a.node}`;
      const anchor = resolveNodePosition(a);
      if (anchor) {
        grp.position.copy(anchor);
      } else {
        // No bone match — stage the un-resolvable effect on a raised
        // platform above the ship so the authoring data is clearly
        // visible. We arrange the unresolved effects in a grid:
        //
        //   Y = 60m   (well above the highest mast at ~46m on Montana)
        //   X spread = -30..+30m  (covers the typical hull beam)
        //   Z spread = -120..+120m (covers the hull length)
        //
        // Indexed deterministically so the same effect always lands at
        // the same point — easier to reason about while inspecting.
        const colCount = 6;
        const col = i % colCount;
        const row = Math.floor(i / colCount);
        const x = (col - (colCount - 1) / 2) * 8;
        const z = (row - 5) * 8;
        grp.position.set(x, 60, z);
      }
      this.root.add(grp);
      // Assigned once below, but the spawn callback closure must capture the
      // binding before that assignment — so it cannot be const.
      // eslint-disable-next-line prefer-const
      let handle: ParticleAttachmentHandle;
      const instantiated = this.instantiateRecordSystems(
        rec,
        grp,
        false,
        options.loopOneShot ? options.loopOneShot(a) : true,
        (request) => {
          void this.spawnChildEffect(handle, grp, request, 0);
        },
      );
      handle = {
        attachment: a,
        group: grp,
        systems: instantiated.systems,
        lights: instantiated.lights,
        record: rec,
        active: false,
      };
      const key = `${a.group}:${a.node}:${i}`;
      this.attachments.set(key, handle);
      handles.push(handle);
    }
    const pointLights = handles
      .flatMap((h) => h.lights)
      .sort((a, b) => b.score - a.score)
      .slice(0, PARTICLE_POINT_LIGHT_BUDGET);
    for (const l of pointLights) l.enablePointLight();
    return handles;
  }

  private instantiateRecordSystems(
    rec: ParticleRecord,
    group: THREE.Group,
    active: boolean,
    loopOneShot: boolean,
    spawnEffect?: ParticleEffectSpawnCallback,
    intensityValues?: readonly number[],
  ): { systems: SystemRenderer[]; lights: LightRenderer[] } {
    const systems: SystemRenderer[] = [];
    const lights: LightRenderer[] = [];
    const intensityDefaults = intensityDefaultsForRecord(rec);
    // Effect-level one-shot loop clock: window + the LONGEST system maxAge,
    // shared by all systems so the looped re-burst stays synchronized (the
    // engine restarts an effect as a unit). Mirrors the constructor's maxAge
    // clamp so the boundary can never undercut a system's own decay window.
    let longestMaxAge = 0;
    for (const sys of rec.systems) {
      longestMaxAge = Math.max(
        longestMaxAge,
        Math.max(0.05, sys.general?.maxParticleAge ?? DEFAULT_PARTICLE_LIFETIME),
      );
    }
    const loopResetPeriod =
      (rec.maxEmittingDuration ?? 0) > 0 ? rec.maxEmittingDuration! + longestMaxAge : 0;
    for (const sys of rec.systems) {
      const r = sys.renderer;
      const anim = sys.animation;
      const systemParent = systemUsesDetachedCoordinateFrame(sys) ? this.root : group;
      // Texture source: prefer the direct DDS URL when present (the
      // texture was extracted as its own file); otherwise route through the
      // manifest atlas mapping. Both paths compose with the animation grid.
      const useAtlas = !r?.textureUrl0 && !!r?.textureAtlas0;
      const useLut = !!r?.blendType && PS_RBT_LUT_MODES.has(r.blendType) && !!r?.textureUrl1;
      const useMv = anim?.animationType === 'motionVectors' && !!anim?.motionVectorsTextureUrl;
      // Sprite rotation has two byte-proven sources (see tick()): a
      // yawRateRamp scaled by spinRateBase, and a standalone spinRateRange
      // drift. spinRateBase alone is NOT a source (default 1.0 only scales
      // the ramp), so it's excluded; spinRateRange IS. A fixed start angle
      // (initialOrientation) or velocityOriented also need the rotated-UV path.
      const hasAuthoredRotation =
        rampHasNonZeroValue(r?.yawRateRamp) ||
        hasNonZeroNumber(r?.spinRateRange) ||
        hasNonZeroNumber(r?.initialOrientationBase) ||
        hasNonZeroNumber(r?.initialOrientationRange) ||
        !!r?.velocityOriented;
      const useSpriteRotation = (!!r?.textureUrl0 || useAtlas) && hasAuthoredRotation;
      const mat = buildParticleMaterial({
        blendType: r?.blendType,
        lightingType: r?.lightingType,
        framesPerX: anim?.framesPerX,
        framesPerY: anim?.framesPerY,
        framesRangeBegin: anim?.framesRangeBegin,
        framesRangeEnd: anim?.framesRangeEnd,
        animationPeriod: anim?.animationPeriod,
        animationType: anim?.animationType,
        atlasRect: useAtlas ? r!.textureAtlas0!.rect : undefined,
        useLut,
        motionVectorsDistortion: useMv ? anim?.motionVectorsDistortion : undefined,
        // Authored flag, NOT gated on the MV texture: it also drives the
        // SHIMMER emission-body composite (which falls back to the lit _LM
        // when no _MVEA is loaded). All mvMap samples are guarded on useMv.
        useEmissionAlphaFromMV: anim?.useEmissionAlphaFromMV,
        deformSigned: r?.textureName0?.startsWith('particles/deform16f/'),
        randomFrameOnly: anim?.randomFrameOnly,
        frameRateRamp: anim?.frameRateRamp,
        spriteRotation: useSpriteRotation,
        rotationCenter: r?.rotationCenter,
        customCenterOffset: r?.customCenterOffset,
        scaleX: r?.scaleX,
        opacityMultiplier: r?.opacityMultiplier,
        tilingU: r?.tilingU,
        tilingV: r?.tilingV,
        flipTexcoordU: r?.flipTexcoordU,
        flipTexcoordV: r?.flipTexcoordV,
        velocityOriented: r?.velocityOriented,
        billboard: r?.billboard,
        lightingAmbient: r?.lightingAmbient,
        lightingDiffuse: r?.lightingDiffuse,
        lightingTransmission: r?.lightingTransmission,
        lightWrapAmount: r?.lightWrapAmount,
        shadowsStrength: r?.shadowsStrength,
        explicitOrientation: r?.explicitOrientation,
        explicitOrientationLocal: r?.explicitOrientationLocal,
        hideStartCos: r?.hideStartCos,
        hideSpeed: r?.hideSpeed,
        softParticleDepthScale: r?.softParticleDepthScale,
        sunDirection: this.sunDirection,
        sunColorNorm: this.sunColorNorm,
      });
      this.applySunLighting(mat);
      const renderer = new SystemRenderer(sys, mat, rec.maxEmittingDuration, {
        spawnEffect,
        loopOneShot,
        loopResetPeriod,
        intensityDefaults,
        sourceGroup: group,
        rootGroup: this.root,
      });
      renderer.setSortCamera(this.sortCamera);
      if (intensityValues) renderer.setIntensityValues(intensityValues);
      renderer.setActive(active);
      systemParent.add(renderer.points);
      systems.push(renderer);
      for (const c of sys.components ?? []) {
        if (c.kind !== 'light' || !c.body) continue;
        const light = new LightRenderer(c.body, sys.intensities?.channels ?? [], intensityDefaults);
        light.ownerSystemIndex = systems.length - 1;
        if (intensityValues) light.setIntensityValues(intensityValues);
        light.setActive(active);
        if (systemParent !== group) {
          group.updateWorldMatrix(true, false);
          systemParent.updateWorldMatrix(true, false);
          group.localToWorld(light.group.position);
          systemParent.worldToLocal(light.group.position);
        }
        systemParent.add(light.group);
        lights.push(light);
      }
      const texPath = r?.textureUrl0 ?? (useAtlas ? r!.textureAtlas0!.page : undefined);
      if (texPath) {
        void this.bindTexture(mat, texPath);
      }
      if (useLut && r?.textureUrl1) {
        void this.bindLutTexture(mat, r.textureUrl1);
      }
      if (useMv && anim?.motionVectorsTextureUrl) {
        void this.bindMvTexture(mat, anim.motionVectorsTextureUrl);
      }
    }
    return { systems, lights };
  }

  private async loadParticleRecord(
    path: string,
    quality: ParticleQuality,
  ): Promise<ParticleRecord | null> {
    const normalized = normalizeParticleEffectPath(path);
    const key = particleRecordCacheKey(normalized, quality);
    const cached =
      this.particleRecords.get(key) ??
      (quality === 'high' ? this.particleRecords.get(normalized) : undefined);
    if (cached) return cached;
    let pending = this.particleRecordFetches.get(key);
    if (!pending) {
      pending = fetchParticleRecord(normalized, quality).catch((err) => {
        console.warn('[particles] child effect record load failed', normalized, quality, err);
        return null;
      });
      this.particleRecordFetches.set(key, pending);
    }
    const record = await pending;
    if (record) {
      this.particleRecords.set(key, record);
      if (quality === 'high') this.particleRecords.set(normalized, record);
    }
    return record;
  }

  private applySunLighting(material: THREE.ShaderMaterial): void {
    const dir = material.uniforms.uSunDirWorld?.value;
    if (dir instanceof THREE.Vector3) dir.copy(this.sunDirection);
    const color = material.uniforms.uSunColorNorm?.value;
    if (color instanceof THREE.Color) color.copy(this.sunColorNorm);
  }

  private updateViewportHeightUniforms(): void {
    if (!this.renderer) return;
    this.renderer.getDrawingBufferSize(this.viewportSize);
    const height = Math.max(1, this.viewportSize.y);
    const apply = (system: SystemRenderer) => {
      const uniform = system.material.uniforms.uViewportHeight;
      if (uniform) uniform.value = height;
    };
    for (const handle of this.attachments.values()) {
      for (const system of handle.systems) apply(system);
    }
    for (const effect of this.spawnedEffects) {
      for (const system of effect.systems) apply(system);
    }
  }

  private async spawnChildEffect(
    parent: ParticleAttachmentHandle,
    parentGroup: THREE.Group,
    request: ParticleEffectSpawnRequest,
    depth: number,
  ): Promise<void> {
    if (!parent.active || depth >= CHILD_EFFECT_DEPTH_LIMIT) return;
    if (this.spawnedEffects.length >= CHILD_EFFECT_BUDGET) return;
    const effectRef = parseParticleEffectRef(request.effectName);
    const effectPath = effectRef.path;
    if (!effectPath.endsWith('.xml')) return;
    const rec = await this.loadParticleRecord(effectPath, effectRef.quality);
    if (!rec || !parent.active) return;

    const group = new THREE.Group();
    group.name = `spawn:${effectPath}`;
    group.position.set(request.position[0], request.position[1], request.position[2]);
    parentGroup.add(group);
    const spawned: SpawnedParticleEffect = {
      parent,
      group,
      systems: [],
      lights: [],
      depth: depth + 1,
    };
    const instantiated = this.instantiateRecordSystems(
      rec,
      group,
      parent.active,
      false,
      (nextRequest) => {
        void this.spawnChildEffect(parent, group, nextRequest, spawned.depth);
      },
      parent.intensityValues,
    );
    spawned.systems = instantiated.systems;
    spawned.lights = instantiated.lights;
    this.spawnedEffects.push(spawned);
  }

  private disposeSpawnedEffect(effect: SpawnedParticleEffect): void {
    for (const s of effect.systems) s.dispose();
    for (const l of effect.lights) l.dispose();
    effect.group.parent?.remove(effect.group);
  }

  private pruneFinishedSpawnedEffects(): void {
    for (let i = this.spawnedEffects.length - 1; i >= 0; i--) {
      const effect = this.spawnedEffects[i];
      if (!effect.systems.every((s) => s.isFinished)) continue;
      this.disposeSpawnedEffect(effect);
      this.spawnedEffects.splice(i, 1);
    }
  }

  /** Resolve a workspace-relative DDS path through the texture cache
   *  and bind it onto `material.uniforms.map`. Idempotent per URL. */
  private async bindTexture(
    material: THREE.ShaderMaterial,
    workspaceRelPath: string,
  ): Promise<void> {
    const r = this.renderer;
    if (!r) return;
    const url = repoUrl(workspaceRelPath);
    let pending = this.textureCache.get(url);
    if (!pending) {
      pending = loadDdsSoftwareRgbaTexture(url, false, r)
        .catch(() => null)
        .then((tex) => tex ?? loadDdsMipChain([url], false, r))
        .catch((err) => {
          console.warn('[particles] DDS load failed', workspaceRelPath, err);
          return null;
        });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.map.value = tex;
    material.uniforms.useMap.value = 1;
    material.needsUpdate = true;
  }

  /** Bind ``workspaceRelPath`` as the LUT sampler used by GRADIENT_MAP /
   *  UNDERWATER_GRADIENT_MAP. Same cache as ``bindTexture`` — many fire
   *  systems share the same ``fire_yellow_*.dds`` ramp. */
  private async bindLutTexture(
    material: THREE.ShaderMaterial,
    workspaceRelPath: string,
  ): Promise<void> {
    const r = this.renderer;
    if (!r) return;
    const url = repoUrl(workspaceRelPath);
    let pending = this.textureCache.get(url);
    if (!pending) {
      pending = loadDdsMipChain([url], false, r).catch((err) => {
        console.warn('[particles] LUT DDS load failed', workspaceRelPath, err);
        return null;
      });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.lut.value = tex;
    material.uniforms.useLut.value = 1;
    material.needsUpdate = true;
  }

  /** Bind ``workspaceRelPath`` as the motion-vector sampler (`_MVEA`) for
   *  the motionVectors animation path. Loaded LINEAR (sRGB=false) — its
   *  (G,B) channels are signed optical-flow data, not colour, so an sRGB
   *  curve would corrupt the (G,B)*2-1 decode. Same cache as bindTexture. */
  private async bindMvTexture(
    material: THREE.ShaderMaterial,
    workspaceRelPath: string,
  ): Promise<void> {
    const r = this.renderer;
    if (!r) return;
    const url = repoUrl(workspaceRelPath);
    let pending = this.textureCache.get(url);
    if (!pending) {
      pending = loadDdsSoftwareRgbaTexture(url, false, r)
        .catch(() => null)
        .then((tex) => tex ?? loadDdsMipChain([url], false, r))
        .catch((err) => {
          console.warn('[particles] MV DDS load failed', workspaceRelPath, err);
          return null;
        });
      this.textureCache.set(url, pending);
    }
    const tex = await pending;
    if (!tex) return;
    material.uniforms.mvMap.value = tex;
    material.uniforms.useMv.value = 1;
    material.needsUpdate = true;
  }

  /** Step every emitter forward by `dt`. Call this from the render loop. */
  tick(nowMs?: number): void {
    this.updateViewportHeightUniforms();
    const now = nowMs ?? performance.now();
    if (this.lastTickMs < 0) {
      this.lastTickMs = now;
      return;
    }
    const dt = Math.min(0.1, (now - this.lastTickMs) * 0.001); // clamp big gaps
    this.lastTickMs = now;
    if (dt <= 0) return;
    for (const handle of this.attachments.values()) {
      if (!handle.active) continue;
      for (const s of handle.systems) s.tick(dt);
      for (const l of handle.lights) l.tick(dt);
    }
    for (const effect of this.spawnedEffects) {
      if (!effect.parent.active) continue;
      for (const s of effect.systems) s.tick(dt);
      for (const l of effect.lights) l.tick(dt);
    }
    this.pruneFinishedSpawnedEffects();
  }

  /** Toggle one attachment on or off. */
  setAttachmentActive(handle: ParticleAttachmentHandle, active: boolean): void {
    handle.active = active;
    for (const s of handle.systems) s.setActive(active);
    for (const l of handle.lights) l.setActive(active);
    for (const effect of this.spawnedEffects) {
      if (effect.parent !== handle) continue;
      for (const s of effect.systems) s.setActive(active);
      for (const l of effect.lights) l.setActive(active);
      effect.group.visible = active;
    }
    handle.group.visible = active;
  }

  restartAttachment(handle: ParticleAttachmentHandle): void {
    for (let i = this.spawnedEffects.length - 1; i >= 0; i--) {
      const effect = this.spawnedEffects[i];
      if (effect.parent !== handle) continue;
      this.disposeSpawnedEffect(effect);
      this.spawnedEffects.splice(i, 1);
    }
    handle.active = true;
    for (const s of handle.systems) s.restart();
    for (const l of handle.lights) l.restart();
    handle.group.visible = true;
  }

  setAttachmentIntensityValues(
    handle: ParticleAttachmentHandle,
    values: readonly number[] | undefined,
  ): void {
    handle.intensityValues = values ? Array.from(values) : undefined;
    for (const s of handle.systems) s.setIntensityValues(values);
    for (const l of handle.lights) l.setIntensityValues(values);
    for (const effect of this.spawnedEffects) {
      if (effect.parent !== handle) continue;
      for (const s of effect.systems) s.setIntensityValues(values);
      for (const l of effect.lights) l.setIntensityValues(values);
    }
  }

  setAttachmentParentVelocity(
    handle: ParticleAttachmentHandle,
    velocityWorld: THREE.Vector3,
  ): void {
    for (const s of handle.systems) s.setParentVelocityWorld(velocityWorld);
    for (const effect of this.spawnedEffects) {
      if (effect.parent !== handle) continue;
      for (const s of effect.systems) s.setParentVelocityWorld(velocityWorld);
    }
  }

  /** Toggle every attachment on/off. */
  setAllActive(active: boolean): void {
    for (const h of this.attachments.values()) this.setAttachmentActive(h, active);
  }

  clear(): void {
    for (const effect of this.spawnedEffects) this.disposeSpawnedEffect(effect);
    this.spawnedEffects = [];
    for (const handle of this.attachments.values()) {
      for (const s of handle.systems) s.dispose();
      for (const l of handle.lights) l.dispose();
      this.root.remove(handle.group);
    }
    this.attachments.clear();
    this.lastTickMs = -1;
  }

  dispose(): void {
    this.clear();
    // Drop cached textures. Each entry may still be resolving — settle
    // first, then dispose. Failures during settle are already swallowed
    // by `bindTexture`, so we don't need to re-handle them here.
    for (const pending of this.textureCache.values()) {
      void pending.then((tex) => tex?.dispose());
    }
    this.textureCache.clear();
    this.renderer = null;
  }
}
