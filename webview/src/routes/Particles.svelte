<script lang="ts">
  // Particles route — single-particle inspector.
  //
  // Renders ONE Effect record from `assets.bin` in isolation: no ship,
  // no neighbour emitters, no scene context. The goal is render-fidelity
  // diagnostics — does our CPU emitter actually behave the way the
  // parsed authoring data says it should?
  //
  // Layout (3 columns):
  //   left   — particle picker (list of every path in assets.bin)
  //   centre — isolated Three.js scene + transport controls
  //   right  — parsed JSON + decoded-vs-rendered checklist
  //
  // URL: `#/particles` (bare) or `#/particles/<vfs-path>`, e.g.
  // `#/particles/particles/vehicles/Fire_big_2.xml`. The router splits
  // on the first slash only, so embedded `/`s in the path pass through
  // without URL-encoding.

  import { onMount, untrack } from 'svelte';
  import * as THREE from 'three';
  import Search from '@lucide/svelte/icons/search';
  import Play from '@lucide/svelte/icons/play';
  import Pause from '@lucide/svelte/icons/pause';
  import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
  import Repeat from '@lucide/svelte/icons/repeat';
  import StepBack from '@lucide/svelte/icons/step-back';
  import StepForward from '@lucide/svelte/icons/step-forward';
  import XIcon from '@lucide/svelte/icons/x';
  import { Button } from '$lib/components/ui/button';
  import { ApiError, fetchJson } from '$lib/api';
  import { navigate } from '$lib/router';
  import { navState } from '$lib/nav_state.svelte';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import {
    ParticleScene,
    type ParticleAttachmentHandle,
    type ParticleAttachmentState,
  } from '$lib/three/particles';
  import { gunShotIntensity } from '$lib/three/particles/gun-shot-intensity';
  import { DEFAULT_PARTICLE_LIFETIME } from '$lib/three/particles/constants';
  import { createSceneEnvironment, type SceneEnvironment } from '$lib/three/scene';
  import { observeResize } from '$lib/three/resize';
  import { startRenderLoop } from '$lib/three/render_loop';
  import type { ParticleAttachment, ParticleRecord, ParticleSystem } from '$lib/types/sidecar';
  import DdsTexturePreview from '$components/DdsTexturePreview.svelte';

  interface Props {
    /** Particle XML path from the URL (`#/particles/<path>`), or null
     *  when the user is on a different page / hasn't selected one yet. */
    particlePath: string | null;
    /** True iff this is the active route. Page-local keydown handlers
     *  short-circuit when false. */
    active: boolean;
  }
  const { particlePath, active }: Props = $props();

  // ── Listing state ───────────────────────────────────────────────────

  let particleList = $state<string[]>([]);
  let listError = $state<string | null>(null);
  let listLoading = $state(true);
  let filter = $state('');
  let searchEl: HTMLInputElement | null = $state(null);

  // ── Current selection ───────────────────────────────────────────────

  /** Sticky — adopts `particlePath` when non-null; persists across tab
   *  switches via `navState.lastParticlePath`. */
  let selectedPath = $state<string | null>(null);
  $effect(() => {
    if (particlePath) {
      selectedPath = particlePath;
      navState.lastParticlePath = particlePath;
    }
  });

  let activeRecord = $state<ParticleRecord | null>(null);
  let availableQualities = $state<string[]>([]);
  /** The quality the backend actually served — may differ from
   *  `qualityChoice` when the requested variant doesn't exist for this
   *  particle (e.g. user clicks `high` but only `low` is available). */
  let qualityUsed = $state<string | null>(null);
  let textureCount = $state<{ stamped: number; missing: number }>({ stamped: 0, missing: 0 });
  let recordError = $state<string | null>(null);
  let recordLoading = $state(false);
  let qualityChoice = $state<'high' | 'low' | 'shared'>('high');

  // ── Intensity channels ──────────────────────────────────────────────
  // Effect intensity is a GAMEPLAY input (EffectManager.setIntensity), not
  // authored data — the record only carries per-channel defaults. Muzzle
  // effects in particular never play at the authored default 1.0: the shot
  // code drives channel 0 from the gun's caliber (see caliberLawIntensity).
  let intensityValues = $state<number[]>([]);
  let intensityPreset = $state<'authored' | 'caliber' | 'custom'>('authored');
  let caliberMm = $state(420);
  let barrelCount = $state(1);
  let shotModifier = $state(1);
  // Environment multipliers from the saved GK capture; editable because
  // another map/weather configuration can use different lighting values.
  let particleDirectLight = $state(0.25);
  let particleAmbientLight = $state(0.8);
  // GK alphaSetter is a spatial near-camera fade (full opacity at ~42 m),
  // so the former 19 m orbit started inside the fade and hid the smoke.
  const defaultParticleCamera: [number, number, number] = [60, 40, 60];

  // ── Three.js scene ──────────────────────────────────────────────────

  let canvasContainer: HTMLElement | null = $state(null);
  let env: SceneEnvironment | null = null;
  let scene: ParticleScene | null = null;
  let stopResize: (() => void) | null = null;
  let stopLoop: (() => void) | null = null;
  /** Per-system AABB helpers (non-reactive; managed in the render loop). */
  let boundsGroup: THREE.Group | null = null;
  let boundsHelpers: Array<{ box: THREE.Box3; helper: THREE.Box3Helper }> = [];

  /** Per-handle stats updated on the render loop. Drives the readouts
   *  in the centre overlay (alive count + elapsed time). */
  let aliveCount = $state(0);
  let playing = $state(true);

  // ── Timeline ────────────────────────────────────────────────────────
  // The inspector runs the simulation in fixed 1/60 s steps from a seeded
  // random source, so the effect at a given time is always the same frame.
  // Scrubbing back restarts the effect and replays to the target; scrubbing
  // forward just keeps stepping. Effects with camera-distance intensity
  // channels still read the live camera, so orbiting can change a replay.
  const SIM_STEP_S = 1 / 60;
  const SIM_SEED = 0x5eed1234;
  const PLAYBACK_RATES = [0.1, 0.25, 0.5, 1, 2];
  /** Fixed steps simulated since the last restart. */
  let simSteps = 0;
  /** Real time owed to the simulation that does not yet fill a step. */
  let pendingRealS = 0;
  let lastFrameMs = -1;
  /** Time picked on the scrubber, applied on the next frame. */
  let pendingSeekS: number | null = null;
  /** Pointer held on the scrubber: playback waits until release. */
  let scrubbing = false;
  /** Saved effect states by step, so seeking back replays at most
   *  CHECKPOINT_STEPS instead of the whole run. Valid only for the current
   *  build and intensity; cleared when either changes. */
  const CHECKPOINT_STEPS = 30;
  const CHECKPOINT_BUDGET_BYTES = 64 * 1024 * 1024;
  let checkpoints = new Map<number, ParticleAttachmentState>();
  let checkpointBytes = 0;
  /** The clock holds at 0 until the effect's child records have loaded, so
   *  every run (and every checkpoint) sees the same child spawns. */
  let simReady = false;
  let buildToken = 0;
  let simTime = $state(0);
  let timelineEnd = $state(10);
  let loopTimeline = $state(true);
  let playbackRate = $state(1);

  // Per-system bounding-box overlay (inspector diagnostic — wraps each
  // system's live particles in a coloured AABB).
  let showBounds = $state(false);
  /** Indices of systems toggled OFF (hidden) in the inspector. */
  let hiddenSystems = $state<Set<number>>(new Set());
  /** Per-system panel rows (rebuilt each frame: live count + hidden flag). */
  let systemRows = $state<Array<{ i: number; name: string; alive: number; hidden: boolean }>>([]);

  // Inspector backdrop. Occluding / alpha-blended smoke (flak bursts, fire
  // smoke — the GRADIENT_MAP + lightmapping path) is near-invisible against a
  // night sky because it darkens the background rather than adding light; a
  // daylit sky reveals it the way it reads in-game. Additive emissive effects
  // (fire glow, sparks, tracers) read on either. Default to sky so the common
  // smoke effects are visible out of the box; toggle to night for additive-only
  // effects that were authored against a dark scene.
  const BACKDROPS = { sky: 0x8c9fb0, night: 0x0a0c11 } as const;
  let backdrop = $state<'sky' | 'night'>('sky');
  $effect(() => {
    // Read `backdrop` UNCONDITIONALLY so it's always tracked as a dependency.
    // `env?.setBackground(BACKDROPS[backdrop])` short-circuits when `env` is
    // still undefined on the effect's first run (mountViewer assigns env
    // later) — and optional chaining then skips evaluating the argument, so
    // `backdrop` was never read, never tracked, and the effect never re-ran
    // when the SKY/NIGHT toggle changed it.
    const color = BACKDROPS[backdrop];
    env?.setBackground(color);
  });

  /** True when a record has at least one DEFORM_WATER_SURFACE system — a
   *  water-surface distortion (ship wake / splash) that authors a refraction
   *  of the ocean, not a sprite. Without a surface to bend, the distortion
   *  pass has nothing to warp and the wake cards read as flat squares. */
  function recordHasDeformWater(rec: ParticleRecord | null): boolean {
    for (const s of rec?.systems ?? []) {
      if (s.renderer?.blendType === 'DEFORM_WATER_SURFACE') return true;
    }
    return false;
  }
  $effect(() => {
    // Read `activeRecord` UNCONDITIONALLY before the optional-chained env call
    // so it is tracked as a dependency (see the backdrop effect above). The
    // initial state is also applied in mountViewer since `env` is not state.
    const showWater = recordHasDeformWater(activeRecord);
    env?.setWaterPlaneVisible(showWater);
  });

  // ── List fetch ──────────────────────────────────────────────────────

  type ListResponse = {
    ok: boolean;
    particles?: string[];
    count?: number;
    record_count?: number;
    error?: string;
  };

  /** Extract the inner `error` string from an ApiError body, falling
   *  back to the generic message. The backend returns
   *  `{ok:false, error:"..."}` on 4xx/5xx; the bare `err.message` is
   *  just `HTTP 503`. */
  function describeError(err: unknown): string {
    if (err instanceof ApiError) {
      const body = err.body as { error?: string } | null;
      if (body && typeof body.error === 'string' && body.error) return body.error;
      return `${err.message}`;
    }
    if (err instanceof Error) return err.message;
    return String(err);
  }

  async function loadList(): Promise<void> {
    listLoading = true;
    listError = null;
    try {
      const res = await fetchJson<ListResponse>('/api/particles');
      if (!res.ok) {
        listError = res.error ?? 'unknown error';
        particleList = [];
        return;
      }
      particleList = res.particles ?? [];
    } catch (err) {
      listError = describeError(err);
      particleList = [];
    } finally {
      listLoading = false;
    }
  }

  // ── Record fetch ────────────────────────────────────────────────────

  type RecordResponse = {
    ok: boolean;
    path?: string;
    qualities?: string[];
    quality_used?: string | null;
    record?: ParticleRecord;
    textures_stamped?: number;
    textures_missing?: string[];
    error?: string;
  };

  async function loadRecord(path: string, quality: string): Promise<void> {
    recordLoading = true;
    recordError = null;
    try {
      const qs = new URLSearchParams({ path, quality });
      const res = await fetchJson<RecordResponse>(`/api/particles/record?${qs}`);
      if (!res.ok) {
        recordError = res.error ?? 'unknown error';
        activeRecord = null;
        return;
      }
      activeRecord = res.record ?? null;
      initIntensityForRecord(activeRecord, path);
      availableQualities = res.qualities ?? [];
      qualityUsed = res.quality_used ?? null;
      textureCount = {
        stamped: res.textures_stamped ?? 0,
        missing: (res.textures_missing ?? []).length,
      };
    } catch (err) {
      recordError = describeError(err);
      activeRecord = null;
    } finally {
      recordLoading = false;
    }
  }

  // ── Intensity presets ───────────────────────────────────────────────

  /** Authored per-channel defaults (record.intensityChannels[].defaultIntensity). */
  function authoredIntensityDefaults(rec: ParticleRecord | null): number[] {
    return (rec?.intensityChannels ?? []).map((c) =>
      Number.isFinite(c.defaultIntensity) ? Number(c.defaultIntensity) : 1,
    );
  }

  /** Match the firing mount's caliber, barrel count and effect modifier. */
  function caliberLawIntensity(mm: number): number {
    return gunShotIntensity(mm, barrelCount, shotModifier);
  }

  /** The shot code sets ONE effect-level value —
   *  EffectManager.setIntensity(effectId, shotIntensity) with no channel
   *  arg (→ channel 0); the other channels keep their authored defaults. */
  function caliberPresetValues(rec: ParticleRecord | null, mm: number): number[] {
    const values = authoredIntensityDefaults(rec);
    if (values.length > 0) values[0] = caliberLawIntensity(mm);
    return values;
  }

  /** Gun-shot records default to the caliber-law preset: in game the muzzle
   *  effect is ALWAYS re-intensified from the firing gun's caliber, so the
   *  authored default 1.0 is a state the engine never renders. */
  function initIntensityForRecord(rec: ParticleRecord | null, path: string): void {
    if (path.startsWith('particles/guns/')) {
      intensityPreset = 'caliber';
      intensityValues = caliberPresetValues(rec, caliberMm);
    } else {
      intensityPreset = 'authored';
      intensityValues = authoredIntensityDefaults(rec);
    }
  }

  function applyIntensity(): void {
    if (scene && currentHandle) scene.setAttachmentIntensityValues(currentHandle, intensityValues);
    // Saved states carry the old intensity; replays from them would mix both.
    clearCheckpoints();
  }

  function setPresetAuthored(): void {
    intensityPreset = 'authored';
    intensityValues = authoredIntensityDefaults(activeRecord);
    applyIntensity();
  }

  function setPresetCaliber(): void {
    intensityPreset = 'caliber';
    intensityValues = caliberPresetValues(activeRecord, caliberMm);
    applyIntensity();
  }

  function onCaliberInput(mm: number): void {
    if (!Number.isFinite(mm)) return;
    caliberMm = mm;
    if (intensityPreset === 'caliber') {
      intensityValues = caliberPresetValues(activeRecord, caliberMm);
      applyIntensity();
    }
  }

  function onShotParameters(barrels: number, modifier: number): void {
    if (!Number.isFinite(barrels) || !Number.isFinite(modifier)) return;
    barrelCount = Math.max(1, Math.trunc(barrels));
    shotModifier = Math.max(0, modifier);
    if (intensityPreset === 'caliber') setPresetCaliber();
  }

  function onParticleLighting(direct: number, ambient: number): void {
    if (!Number.isFinite(direct) || !Number.isFinite(ambient)) return;
    particleDirectLight = Math.max(0, direct);
    particleAmbientLight = Math.max(0, ambient);
    scene?.setLightingFactors(particleDirectLight, particleAmbientLight);
  }

  function onChannelSlider(i: number, v: number): void {
    const next = intensityValues.slice();
    next[i] = v;
    intensityValues = next;
    intensityPreset = 'custom';
    applyIntensity();
  }

  // Auto-refetch whenever the path or quality changes.
  $effect(() => {
    const p = selectedPath;
    const q = qualityChoice;
    if (!p) {
      activeRecord = null;
      recordError = null;
      return;
    }
    void loadRecord(p, q);
  });

  // ── Three.js lifecycle ──────────────────────────────────────────────

  function resetView(): void {
    if (!env) return;
    env.camera.position.set(...defaultParticleCamera);
    env.controls.target.set(0, 0, 0);
    env.controls.update();
  }

  function mountViewer(container: HTMLElement) {
    // Compact-ish scene: camera close to the origin, smaller grid, no
    // axes hint (the inspector doesn't care about world axes — the
    // emitter is at (0,0,0) by construction).
    env = createSceneEnvironment(container, {
      background: 0x0a0c11,
      cameraPosition: defaultParticleCamera,
      gridSize: 120,
      gridDivisions: 20,
      axesSize: 1.5,
      far: 1000,
    });
    env.setBloomEnabled(true);
    env.setBloomParams({ strength: 0.28, radius: 0.25, threshold: 1.35 });
    env.setBackground(BACKDROPS[backdrop]);
    env.setWaterPlaneVisible(recordHasDeformWater(activeRecord));
    // Every inspected effect spawns at the origin, so the axes helper sits
    // right inside the effect — and its bright-green Y axis blooms into a beam
    // that reads as part of the particle. Hide it (the grid still gives scale).
    env.axes.visible = false;

    scene = new ParticleScene(env.renderer);
    scene.setLightingFactors(particleDirectLight, particleAmbientLight);
    scene.setSortCamera(env.camera);
    const sun = env.getSunLight();
    scene.setSunLighting(sun.direction, sun.color);
    env.scene.add(scene.root);

    // Console/automation handle, like `__shipViewer__` on the ship page:
    //   __particleInspector__.seek(1.5); __particleInspector__.handle.systems
    (globalThis as { __particleInspector__?: unknown }).__particleInspector__ = {
      get scene() {
        return scene;
      },
      get env() {
        return env;
      },
      get handle() {
        return currentHandle;
      },
      get time() {
        return simSteps * SIM_STEP_S;
      },
      get checkpoints() {
        return { count: checkpoints.size, bytes: checkpointBytes };
      },
      seek: (seconds: number) => (pendingSeekS = seconds),
      pause: () => (playing = false),
      play: () => (playing = true),
    };

    boundsGroup = new THREE.Group();
    boundsGroup.name = 'ParticleSystemBounds';
    env.scene.add(boundsGroup);

    stopResize = observeResize({
      container,
      renderer: env.renderer,
      camera: env.camera,
      onResize: (w, h) => env?.setSize(w, h),
    });

    stopLoop = startRenderLoop(() => {
      env!.controls.update();
      const now = performance.now();
      const realDt = lastFrameMs < 0 ? 0 : Math.min(0.1, (now - lastFrameMs) * 0.001);
      lastFrameMs = now;
      if (!simReady) {
        scene!.advanceFixed(0, SIM_STEP_S);
      } else if (pendingSeekS !== null) {
        seekSimulation(pendingSeekS);
        pendingSeekS = null;
      } else if (playing && !scrubbing && currentHandle) {
        advancePlayback(realDt);
      } else {
        // No steps, but keep the draw order fresh while the camera moves.
        scene!.advanceFixed(0, SIM_STEP_S);
      }
      syncSystems();
      env!.render();
      // Mirror the alive count and timeline position into reactive state so
      // the readouts update without a manual ping.
      simTime = simSteps * SIM_STEP_S;
      if (!loopTimeline && simTime > timelineEnd) timelineEnd = simTime;
      let total = 0;
      for (const s of currentHandle?.systems ?? []) total += s.aliveCount;
      aliveCount = total;
    });
  }

  function disposeViewer() {
    stopLoop?.();
    stopResize?.();
    scene?.dispose();
    env?.dispose();
    stopLoop = null;
    stopResize = null;
    scene = null;
    env = null;
    currentHandle = null;
    for (const { helper } of boundsHelpers) {
      helper.geometry.dispose();
      (helper.material as THREE.Material).dispose();
    }
    boundsHelpers = [];
    boundsGroup = null;
    systemRows = [];
    hiddenSystems = new Set();
  }

  // ── Per-system bounds overlay ───────────────────────────────────────
  const BOUNDS_HUE_STEP = 0.618033988749895; // golden-ratio hue spread

  function boundsColor(i: number): THREE.Color {
    return new THREE.Color().setHSL((0.08 + i * BOUNDS_HUE_STEP) % 1, 0.85, 0.6);
  }
  function boundsColorHex(i: number): string {
    return '#' + boundsColor(i).getHexString();
  }

  /** Toggle a system's visibility (its sprites, its lights, and its AABB).
   *  Reassigns the Set so Svelte re-renders the panel. */
  function toggleSystem(i: number): void {
    const next = new Set(hiddenSystems);
    if (next.has(i)) next.delete(i);
    else next.add(i);
    hiddenSystems = next;
  }

  /** Apply per-system visibility (sprite mesh + the lights that system
   *  authored), refresh the AABB helpers, and publish the panel rows. Called
   *  each frame BEFORE render so visibility + Box3Helper matrices are fresh.
   *  The helper pool is sized to the system count; a box draws only when the
   *  overlay is on, the system is visible, and it has live particles. */
  function syncSystems() {
    const systems = currentHandle ? currentHandle.systems : [];
    // 1. Visibility — sprite mesh + the lights that system authored.
    for (let i = 0; i < systems.length; i++) {
      systems[i].setDrawVisible(!hiddenSystems.has(i));
    }
    if (currentHandle) {
      for (const l of currentHandle.lights) {
        l.group.visible = l.ownerSystemIndex < 0 || !hiddenSystems.has(l.ownerSystemIndex);
      }
    }
    // 2. Size the AABB helper pool to the system count.
    if (boundsGroup) {
      while (boundsHelpers.length < systems.length) {
        const idx = boundsHelpers.length;
        const box = new THREE.Box3();
        const helper = new THREE.Box3Helper(box, boundsColor(idx));
        const mat = helper.material as THREE.LineBasicMaterial;
        mat.depthTest = false;
        mat.transparent = true;
        helper.renderOrder = 999;
        helper.visible = false;
        boundsGroup.add(helper);
        boundsHelpers.push({ box, helper });
      }
      while (boundsHelpers.length > systems.length) {
        const last = boundsHelpers.pop()!;
        boundsGroup.remove(last.helper);
        last.helper.geometry.dispose();
        (last.helper.material as THREE.Material).dispose();
      }
    }
    // 3. Per-frame box update + panel rows.
    const rows: Array<{ i: number; name: string; alive: number; hidden: boolean }> = [];
    for (let i = 0; i < systems.length; i++) {
      const hidden = hiddenSystems.has(i);
      const slot = boundsHelpers[i];
      if (slot) {
        slot.helper.visible = showBounds && !hidden && systems[i].computeWorldBounds(slot.box);
      }
      rows.push({ i, name: systems[i].name, alive: systems[i].aliveCount, hidden });
    }
    systemRows = rows;
  }

  // ── Per-record viewer state ─────────────────────────────────────────

  /** The single fake attachment we render. Path becomes the key into
   *  the synthetic `particles` map fed to ParticleScene.build. */
  let currentHandle: ParticleAttachmentHandle | null = null;

  function rebuildSceneFromRecord(rec: ParticleRecord | null) {
    if (!scene) return;
    scene.clear();
    scene.setRandomSeed(SIM_SEED);
    currentHandle = null;
    hiddenSystems = new Set();
    simSteps = 0;
    pendingRealS = 0;
    pendingSeekS = null;
    clearCheckpoints();
    simReady = false;
    const token = ++buildToken;
    if (!rec || !selectedPath) return;
    const synthetic: ParticleAttachment = {
      group: 'inspector',
      node: 'origin',
      particle_path: selectedPath,
      source: 'hull',
    };
    const handles = scene.build(
      [synthetic],
      { [selectedPath]: rec },
      () => new THREE.Vector3(0, 0, 0),
      { prefetchChildEffects: true },
    );
    if (handles.length === 0) return;
    const h = handles[0];
    scene.setAttachmentActive(h, true);
    scene.setAttachmentIntensityValues(h, intensityValues);
    currentHandle = h;
    // Steps start from the top of the sequence, the same as a seek restart.
    scene.setRandomSeed(SIM_SEED);
    void scene.whenChildEffectsReady().then(() => {
      if (token === buildToken) simReady = true;
    });
  }

  // Rebuild whenever the active record changes. The rebuild also reads
  // `playing` and `intensityValues` — untracked, so toggling play/pause or
  // dragging an intensity slider never resets the simulator.
  $effect(() => {
    const rec = activeRecord;
    untrack(() => {
      timelineEnd = defaultTimelineEnd(rec);
      rebuildSceneFromRecord(rec);
    });
  });

  /** Scrubber length for a record. A one-shot gets its own loop period
   *  (emission window plus the longest particle life, the period the scene
   *  re-bursts on); a continuous effect gets a few particle lifetimes so it
   *  reaches its steady state. */
  function defaultTimelineEnd(rec: ParticleRecord | null): number {
    let longestMaxAge = 0;
    for (const sys of rec?.systems ?? []) {
      const age = sys.general?.maxParticleAge ?? DEFAULT_PARTICLE_LIFETIME;
      longestMaxAge = Math.max(longestMaxAge, Math.max(0.05, age));
    }
    const emitting = rec?.maxEmittingDuration ?? 0;
    const end = emitting > 0 ? emitting + longestMaxAge : Math.max(5, 3 * longestMaxAge);
    return Math.min(120, Math.ceil(end / SIM_STEP_S - 1e-6) * SIM_STEP_S);
  }

  /** Restart the effect and the random sequence: time 0 of the replay. */
  function restartSimulation(): void {
    if (!scene || !currentHandle) return;
    scene.restartAttachment(currentHandle);
    scene.setRandomSeed(SIM_SEED);
    simSteps = 0;
  }

  function clearCheckpoints(): void {
    checkpoints = new Map();
    checkpointBytes = 0;
  }

  /** Save the effect's state at the current step, within the memory budget. */
  function saveCheckpoint(): void {
    if (!scene || !currentHandle || checkpoints.has(simSteps)) return;
    const state = scene.captureAttachmentState(currentHandle);
    if (checkpointBytes + state.bytes > CHECKPOINT_BUDGET_BYTES) return;
    checkpoints.set(simSteps, state);
    checkpointBytes += state.bytes;
  }

  /** Step forward to `target`, saving a checkpoint at each boundary. */
  function advanceTo(target: number): void {
    if (!scene) return;
    if (target <= simSteps) {
      scene.advanceFixed(0, SIM_STEP_S);
      return;
    }
    while (simSteps < target) {
      const mark = (Math.floor(simSteps / CHECKPOINT_STEPS) + 1) * CHECKPOINT_STEPS;
      const stop = Math.min(target, mark);
      scene.advanceFixed(stop - simSteps, SIM_STEP_S);
      simSteps = stop;
      if (stop === mark) saveCheckpoint();
    }
  }

  /** Move the simulation to `seconds`, rounded to a step. Going back
   *  restores the last checkpoint before the target (or restarts) and
   *  replays the rest; going forward continues from the current state. */
  function seekSimulation(seconds: number): void {
    if (!scene || !currentHandle) return;
    const target = Math.max(0, Math.round(seconds / SIM_STEP_S));
    if (target < simSteps) {
      // Strictly before the target, so at least one step runs and redraws.
      let best = 0;
      for (const step of checkpoints.keys()) if (step < target && step > best) best = step;
      const state = best > 0 ? checkpoints.get(best) : undefined;
      if (state && scene.restoreAttachmentState(currentHandle, state)) simSteps = best;
      else restartSimulation();
    }
    advanceTo(target);
    pendingRealS = 0;
  }

  /** Turn elapsed real time into whole simulation steps, wrapping at the
   *  end of the timeline when looping. */
  function advancePlayback(realDt: number): void {
    // Round to the nearest step and carry the signed remainder. Frame times
    // wobble around 16.7 ms, so flooring gave an irregular 1,2,0,1… step
    // pattern at 60 Hz: a frozen frame next to a double step every few
    // frames, which made slow sheets (wake and splash foam) visibly stutter.
    // Rounding locks to one step per display frame while the remainder keeps
    // the long-run rate exact.
    pendingRealS += realDt * playbackRate;
    let steps = Math.max(0, Math.round(pendingRealS / SIM_STEP_S));
    pendingRealS -= steps * SIM_STEP_S;
    const endSteps = Math.round(timelineEnd / SIM_STEP_S);
    if (loopTimeline && endSteps > 0 && simSteps + steps >= endSteps) {
      steps = (simSteps + steps - endSteps) % endSteps;
      restartSimulation();
    }
    advanceTo(simSteps + steps);
  }

  /** Pause and move one or more frames (negative = back). */
  function stepFrames(delta: number): void {
    playing = false;
    pendingSeekS = Math.max(0, simSteps + delta) * SIM_STEP_S;
  }

  function setTimelineEnd(value: number): void {
    if (!Number.isFinite(value) || value <= 0) return;
    timelineEnd = Math.min(600, Math.round(value / SIM_STEP_S) * SIM_STEP_S || SIM_STEP_S);
    if (simSteps * SIM_STEP_S > timelineEnd) pendingSeekS = timelineEnd;
  }

  // ── Lifecycle wiring ────────────────────────────────────────────────

  onMount(() => {
    void loadList();
    if (canvasContainer) mountViewer(canvasContainer);

    const onKey = (e: KeyboardEvent) => {
      if (!active) return;
      if (hasModifier(e)) return;
      if (isTypingContext(e)) return;
      switch (e.key) {
        case '/':
          searchEl?.focus();
          e.preventDefault();
          return;
        case ' ':
        case 'k':
        case 'K':
          playing = !playing;
          e.preventDefault();
          return;
        case 'r':
        case 'R':
          restart();
          e.preventDefault();
          return;
        case ',':
          stepFrames(-1);
          e.preventDefault();
          return;
        case '.':
          stepFrames(1);
          e.preventDefault();
          return;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      disposeViewer();
    };
  });

  // ── Picker actions ──────────────────────────────────────────────────

  function selectPath(path: string) {
    navigate(`#/particles/${path}`);
  }

  function restart() {
    rebuildSceneFromRecord(activeRecord);
    playing = true;
  }

  function clearFilter() {
    filter = '';
    searchEl?.focus();
  }

  // ── Derived ─────────────────────────────────────────────────────────

  const filteredList = $derived.by(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return particleList;
    return particleList.filter((p) => p.toLowerCase().includes(q));
  });

  /** Short, human-friendly form of a particle path. Strips the leading
   *  `particles/` and trailing `.xml`. */
  function shortName(path: string): string {
    return path.replace(/^particles\//, '').replace(/\.xml$/, '');
  }

  /** Set of action names the WEBVIEW emitter (`$lib/three/particles`)
   *  actually consumes today. Anything else in a record is silently
   *  ignored — the inspector flags these so the user knows render
   *  fidelity is limited there.
   *
   *  Source: `webview/src/lib/three/particles/system-renderer.ts` — the
   *  `SystemRenderer` constructor's switch on `c.action`.
   */
  const RENDERED_ACTIONS = new Set([
    'creator',
    'tint',
    'alphaSetter',
    'scaler',
    'resizer',
    'force',
    'dampfer',
    'stream',
    'jitter',
    'orbitor',
    'magnet',
    'spawner',
    'sphere',
    'cylinder',
    'box',
    'plane',
    'velocityField',
    'light',
  ]);

  /** PS_RBT blend modes rendered through the webview's screen-space
   *  distortion approximation. Still not the full native water/heat-haze
   *  technique, but no longer a plain alpha-over placeholder. */
  const PLACEHOLDER_BLEND_MODES = new Set(['SHIMMER', 'DEFORM_WATER_SURFACE']);

  /** Render-fidelity caveats that actually apply to THIS record, keyed off
   *  the decoded data rather than guessed from filenames.
   *
   *  Note: `framesPerX/Y` (animation grid) and `blendType` (PS_RBT, 10
   *  values) ARE decoded by the parser (`read/particles.py`
   *  _decode_animation / _decode_renderer) and consumed by the renderer
   *  (`three/particles/material.ts` `blendConfigForPsRbt` + the flipbook-UV shader
   *  math) since the 2026-05-23 gap-closing pass — they are no longer gaps.
   *  What remains:
   *    - a few PS_RBT modes (SHIMMER / DEFORM_WATER_SURFACE) use a
   *      screen-space refraction approximation instead of the full native
   *      water/heat-haze technique;
   *    - a texture ref that resolved to neither a direct extract
   *      (`textureUrl0`) nor an atlas region (`textureAtlas0`) renders as a
   *      procedural soft disc. */
  function renderCaveatsForRecord(rec: ParticleRecord | null): string[] {
    if (!rec) return [];
    const gaps: string[] = [];
    const placeholderBlends = new Set<string>();
    let anyUnresolvedTexture = false;
    let anyLightmap = false;
    let anyMvEmission = false;
    let anyWaterSurfaceColor = false;
    for (const s of rec.systems ?? []) {
      const r = s.renderer;
      if (!r) continue;
      if (r.blendType && PLACEHOLDER_BLEND_MODES.has(r.blendType)) {
        placeholderBlends.add(r.blendType);
      }
      if (r.blendType === 'BLENDED_WATER_SURFACE' || r.blendType === 'ADDITIVE_WATER_SURFACE') {
        anyWaterSurfaceColor = true;
      }
      // textureName0 referenced, but it resolved to neither a direct DDS
      // extract nor an atlas region -> the renderer draws a procedural disc.
      if (r.textureName0 && !r.textureUrl0 && !r.textureAtlas0) {
        anyUnresolvedTexture = true;
      }
      // Directional lightmap (`_LM`): relit approximately (see below).
      if (r.lightingType === 'lightmapping4Way' || r.lightingType === 'lightmappingHL2') {
        anyLightmap = true;
      }
      // `_MVEA` motion-vector blend is applied; emission-from-MV now follows
      // the native non-gradient substitution rule.
      if (s.animation?.useEmissionAlphaFromMV) anyMvEmission = true;
    }
    if (placeholderBlends.has('DEFORM_WATER_SURFACE')) {
      gaps.push(
        'DEFORM_WATER_SURFACE: with the water plane on, foam and height are drawn into a water layer ' +
          'the ocean reads (native sea composition, height scale not calibrated); without it, flat ' +
          'foam cards stand in',
      );
    }
    if (anyWaterSurfaceColor) {
      gaps.push(
        'BLENDED_/ADDITIVE_WATER_SURFACE: with the water plane on, drawn into a colour layer the ' +
          'ocean lays over the waves (native composition; layer blend state inferred, no sea ' +
          'shadows); without it, flat cards stand in',
      );
    }
    if (placeholderBlends.has('SHIMMER')) {
      gaps.push(
        'blend mode SHIMMER approximated as screen-space refraction from a scene-color copy; ' +
          'native heat-haze pass not exact',
      );
    }
    if (anyUnresolvedTexture) {
      gaps.push(
        'texture(s) referenced but resolved to neither a direct extract nor ' +
          'an atlas region — render falls back to a procedural disc',
      );
    }
    if (anyLightmap) {
      gaps.push(
        'directional lightmap (lightingType=lightmapping*) relit approximately ' +
          'from a fixed sun direction (HL2 basis) — not the engine-exact 4-way decode',
      );
    }
    if (anyMvEmission) {
      gaps.push(
        '_MVEA emission (useEmissionAlphaFromMV) substitutes the non-gradient ' +
          'particle body; gradient-map permutations use the authored ramp glow',
      );
    }
    return gaps;
  }

  /** Per-system action checklist. Tags each action `rendered` / `ignored`
   *  so the user can see at a glance which authoring fields the live
   *  emitter is actually consuming. */
  function actionChecklist(
    system: ParticleSystem,
  ): Array<{ kind: string; action: string; rendered: boolean }> {
    const out: Array<{ kind: string; action: string; rendered: boolean }> = [];
    for (const c of system.components ?? []) {
      const action = c.action ?? c.kind ?? '?';
      out.push({
        kind: c.kind ?? '?',
        action,
        rendered: RENDERED_ACTIONS.has(action),
      });
    }
    return out;
  }

  const repoBase = $derived(
    typeof window !== 'undefined' ? `${window.location.origin}/repo/` : '/repo/',
  );

  /** First textureUrl0 in any system, used for the side-panel preview. */
  const firstTexture = $derived.by(() => {
    for (const s of activeRecord?.systems ?? []) {
      const url = s.renderer?.textureUrl0;
      const name = s.renderer?.textureName0;
      if (url) return { url, name };
    }
    return null;
  });
</script>

<div class="flex flex-1 min-w-0 h-full">
  <!-- ── Left: picker ─────────────────────────────────────────────── -->
  <aside class="bg-card border-border flex w-72 flex-none flex-col border-r">
    <div class="flex-none border-b border-border px-3 py-2">
      <div class="flex items-center justify-between">
        <span class="text-foreground text-xs font-semibold tracking-wider uppercase">
          particles
        </span>
        <span class="text-muted-foreground text-[10px] tabular-nums">
          {filteredList.length}/{particleList.length}
        </span>
      </div>
      <div class="relative mt-1.5">
        <Search class="text-muted-foreground pointer-events-none absolute left-2 top-1.5 size-3" />
        <input
          bind:this={searchEl}
          type="search"
          value={filter}
          oninput={(e) => (filter = e.currentTarget.value)}
          placeholder="Filter (e.g. Fire_big)…"
          aria-label="Filter particle list"
          class="bg-popover text-foreground border-border placeholder:text-muted-foreground focus:ring-ring/30 h-7 w-full rounded border pl-7 pr-6 text-xs outline-none focus:border-ring focus:ring-2 [&::-webkit-search-cancel-button]:hidden"
        />
        {#if filter}
          <button
            type="button"
            class="text-muted-foreground hover:bg-popover hover:text-foreground absolute right-1 top-1 flex size-[18px] items-center justify-center rounded"
            onclick={clearFilter}
            aria-label="Clear filter"
          >
            <XIcon class="size-3" />
          </button>
        {/if}
      </div>
    </div>
    <div class="flex-1 min-h-0 overflow-y-auto">
      {#if listLoading}
        <div class="text-muted-foreground px-3 py-3 text-[11px]">loading…</div>
      {:else if listError}
        <div class="text-destructive px-3 py-3 text-[11px]">
          {listError}
        </div>
      {:else if filteredList.length === 0}
        <div class="text-muted-foreground px-3 py-3 text-[11px]">no matches</div>
      {:else}
        <ul class="flex flex-col">
          {#each filteredList as p (p)}
            <li>
              <button
                type="button"
                onclick={() => selectPath(p)}
                title={p}
                class="block w-full truncate px-3 py-1 text-left font-mono text-[11px] hover:bg-muted/30 {selectedPath ===
                p
                  ? 'bg-primary/15 border-l-2 border-l-primary text-foreground'
                  : 'text-muted-foreground border-l-2 border-l-transparent'}"
              >
                {shortName(p)}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </aside>

  <!-- ── Centre: scene + controls ─────────────────────────────────── -->
  <!-- overflow-hidden: this section is positioned, so anything spilling out
       of it would paint over the inspector panel. -->
  <section class="relative flex flex-1 min-w-0 flex-col overflow-hidden">
    <!-- Toolbar: the name/path truncate first; when the column is too narrow
         for the controls they wrap onto a second row. -->
    <div
      class="bg-card border-border flex flex-none flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2 text-xs"
    >
      <div class="flex min-w-0 flex-1 basis-40 items-center gap-2">
        {#if selectedPath}
          <code class="text-foreground min-w-0 truncate font-mono" title={shortName(selectedPath)}
            >{shortName(selectedPath)}</code
          >
          <span class="text-muted-foreground/60 flex-none">·</span>
          <code
            class="text-muted-foreground min-w-0 shrink-[2] truncate font-mono text-[10px]"
            title={selectedPath}>{selectedPath}</code
          >
        {:else}
          <span class="text-muted-foreground truncate">no particle selected</span>
        {/if}
      </div>

      <div class="ml-auto flex flex-wrap items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          onclick={resetView}
          title="Frame the emitter outside GK's near-camera fade zone. Zooming close applies authored distance fading."
        >
          Reset view
        </Button>
        <div
          class="flex rounded border border-border overflow-hidden"
          title="Inspector backdrop — occluding smoke (flak/fire smoke) needs a lit sky to be visible; additive effects read on either"
        >
          {#each ['sky', 'night'] as const as bg (bg)}
            <button
              type="button"
              onclick={() => (backdrop = bg)}
              class="px-2 py-0.5 text-[10px] font-medium tracking-wider uppercase {backdrop === bg
                ? 'bg-primary text-primary-foreground'
                : 'bg-popover text-muted-foreground hover:text-foreground'}"
            >
              {bg}
            </button>
          {/each}
        </div>
        <button
          type="button"
          onclick={() => (showBounds = !showBounds)}
          title="Toggle per-system bounding boxes"
          class="rounded border border-border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider {showBounds
            ? 'bg-primary text-primary-foreground'
            : 'bg-popover text-muted-foreground hover:text-foreground'}"
        >
          boxes
        </button>
        {#if availableQualities.length > 1}
          <div class="flex rounded border border-border overflow-hidden">
            {#each availableQualities as q (q)}
              <button
                type="button"
                onclick={() => (qualityChoice = q as 'high' | 'low' | 'shared')}
                title={q === qualityChoice && q !== qualityUsed
                  ? `you picked ${q}, this particle has no ${q} variant — showing ${qualityUsed}`
                  : `quality variant: ${q}`}
                class="px-2 py-0.5 text-[10px] font-medium tracking-wider uppercase {qualityUsed ===
                q
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-popover text-muted-foreground hover:text-foreground'}"
              >
                {q}
              </button>
            {/each}
          </div>
        {/if}
      </div>
    </div>

    <!-- Canvas -->
    <div class="relative flex-1 min-h-0">
      <div bind:this={canvasContainer} class="absolute inset-0"></div>

      {#if !selectedPath}
        <div
          class="text-muted-foreground pointer-events-none absolute inset-0 flex items-center justify-center text-center text-sm"
        >
          Pick a particle from the left to inspect it.
        </div>
      {:else if recordLoading}
        <div
          class="text-muted-foreground pointer-events-none absolute inset-0 flex items-center justify-center text-center text-sm"
        >
          parsing record…
        </div>
      {:else if recordError}
        <div
          class="text-destructive pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-sm"
        >
          {recordError}
        </div>
      {/if}

      <!-- Live overlay: alive count + elapsed -->
      {#if selectedPath && activeRecord && !recordError}
        <div
          class="bg-popover/85 border-border absolute left-3 top-3 rounded border px-2 py-1 font-mono text-[10px] backdrop-blur"
        >
          <div class="text-muted-foreground">
            alive <span class="text-foreground tabular-nums">{aliveCount}</span>
          </div>
          <div class="text-muted-foreground">
            t <span class="text-foreground tabular-nums">{simTime.toFixed(2)}s</span>
          </div>
          <div class="text-muted-foreground">
            systems <span class="text-foreground tabular-nums"
              >{activeRecord.systems?.length ?? 0}</span
            >
          </div>
        </div>
      {/if}

      <!-- Per-system panel: click a row to show/hide that system (its sprites,
           its lights, and its AABB). Swatch ↔ the system's bounding-box colour. -->
      {#if selectedPath && activeRecord && !recordError && systemRows.length > 0}
        <div
          class="bg-popover/85 border-border absolute left-3 top-20 flex max-h-[60%] flex-col overflow-auto rounded border px-1.5 py-1 font-mono text-[10px] backdrop-blur"
        >
          <div class="flex items-center justify-between gap-3 px-0.5 pb-1">
            <span class="text-muted-foreground text-[9px] uppercase tracking-wider">systems</span>
            {#if hiddenSystems.size > 0}
              <button
                type="button"
                class="text-muted-foreground hover:text-foreground text-[9px] uppercase tracking-wider"
                onclick={() => (hiddenSystems = new Set())}
                title="Show all systems"
              >
                show all
              </button>
            {/if}
          </div>
          {#each systemRows as s (s.i)}
            <button
              type="button"
              onclick={() => toggleSystem(s.i)}
              title={`${s.hidden ? 'Show' : 'Hide'} ${s.name ? `"${s.name}" (#${s.i})` : `system #${s.i}`}`}
              class="hover:bg-muted/40 flex items-center gap-1.5 rounded px-0.5 py-[1px] text-left {s.hidden
                ? 'opacity-40'
                : ''}"
            >
              <span
                class="border-border inline-block size-2.5 flex-none rounded-[2px] border"
                style={s.hidden
                  ? ''
                  : `background:${boundsColorHex(s.i)};border-color:${boundsColorHex(s.i)}`}
              ></span>
              <span class="text-muted-foreground flex-none">#{s.i}</span>
              {#if s.name}
                <span class="text-foreground max-w-[130px] truncate">{s.name}</span>
              {/if}
              <span class="text-muted-foreground ml-auto pl-3 tabular-nums">{s.alive}</span>
            </button>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Timeline: scrub, step and loop the simulation clock. -->
    {#if selectedPath && activeRecord && !recordError}
      <div
        class="border-border bg-card flex flex-none items-center gap-1.5 border-t px-2 py-1 font-mono text-[10px]"
      >
        <Button
          variant="ghost"
          size="sm"
          class="h-6 px-1.5"
          onclick={restart}
          title="Restart simulation (R)"
        >
          <RotateCcw class="size-3" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          class="h-6 px-1.5"
          onclick={() => stepFrames(-1)}
          title="Back one frame (,)"
        >
          <StepBack class="size-3" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          class="h-6 px-1.5"
          onclick={() => (playing = !playing)}
          title={playing ? 'Pause (space)' : 'Play (space)'}
        >
          {#if playing}
            <Pause class="size-3" />
          {:else}
            <Play class="size-3" />
          {/if}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          class="h-6 px-1.5"
          onclick={() => stepFrames(1)}
          title="Forward one frame (.)"
        >
          <StepForward class="size-3" />
        </Button>
        <span class="text-foreground w-[52px] flex-none text-right tabular-nums"
          >{simTime.toFixed(2)}s</span
        >
        <input
          type="range"
          class="accent-primary h-1 min-w-0 flex-1 cursor-pointer"
          min="0"
          max={timelineEnd}
          step={SIM_STEP_S}
          value={simTime}
          aria-label="Simulation time"
          title="Drag to scrub the simulation time"
          oninput={(e) => (pendingSeekS = Number(e.currentTarget.value))}
          onpointerdown={() => (scrubbing = true)}
          onpointerup={() => (scrubbing = false)}
          onpointercancel={() => (scrubbing = false)}
          onchange={() => (scrubbing = false)}
        />
        <label
          class="text-muted-foreground flex flex-none items-center gap-1"
          title="Timeline length in seconds"
        >
          /
          <input
            type="number"
            min="0.1"
            step="0.5"
            value={timelineEnd.toFixed(2)}
            onchange={(e) => setTimelineEnd(Number(e.currentTarget.value))}
            class="bg-popover border-border text-foreground w-14 rounded border px-1 py-0.5 tabular-nums"
          />
          s
        </label>
        <button
          type="button"
          onclick={() => (loopTimeline = !loopTimeline)}
          title={loopTimeline
            ? 'Looping at the end of the timeline (click to run on past it)'
            : 'Running past the end of the timeline (click to loop)'}
          class="flex-none rounded border border-border p-1 {loopTimeline
            ? 'bg-primary text-primary-foreground'
            : 'bg-popover text-muted-foreground hover:text-foreground'}"
        >
          <Repeat class="size-3" />
        </button>
        <select
          bind:value={playbackRate}
          title="Playback speed"
          class="bg-popover border-border text-foreground flex-none rounded border px-1 py-0.5"
        >
          {#each PLAYBACK_RATES as rate (rate)}
            <option value={rate}>{rate}×</option>
          {/each}
        </select>
      </div>
    {/if}
  </section>

  <!-- ── Right: parameter inspector ───────────────────────────────── -->
  <aside class="bg-card border-border flex w-[420px] flex-none flex-col border-l">
    <div
      class="flex-none border-b border-border px-3 py-2 text-xs font-semibold tracking-wider uppercase text-foreground"
    >
      inspector
    </div>
    <div class="flex-1 min-h-0 overflow-y-auto p-3 text-[11px]">
      {#if !activeRecord}
        <div class="text-muted-foreground">no record loaded</div>
      {:else}
        {@const gaps = renderCaveatsForRecord(activeRecord)}

        <!-- Render-fidelity caveats -->
        {#if gaps.length > 0}
          <section class="mb-3 rounded border border-amber-900/50 bg-amber-950/30 p-2">
            <div class="text-amber-300 mb-1 text-[10px] uppercase tracking-wider font-semibold">
              render caveats
            </div>
            <ul class="text-amber-200/90 text-[10px] flex flex-col gap-0.5">
              {#each gaps as g (g)}
                <li>· {g}</li>
              {/each}
            </ul>
          </section>
        {/if}

        <section class="border-border bg-muted/20 mb-3 rounded border p-2">
          <div class="text-muted-foreground mb-1 text-[9px] uppercase tracking-wider">
            reference lighting
          </div>
          <p class="text-muted-foreground mb-2 text-[10px]">Defaults from the saved GK capture.</p>
          <div class="flex flex-wrap gap-2 font-mono text-[10px]">
            <label class="flex items-center gap-1">
              direct
              <input
                type="number"
                min="0"
                step="0.05"
                value={particleDirectLight}
                oninput={(e) =>
                  onParticleLighting(Number(e.currentTarget.value), particleAmbientLight)}
                class="border-border bg-background w-14 rounded border px-1 py-0.5 text-right"
                aria-label="Particle direct lighting factor"
              />
            </label>
            <label class="flex items-center gap-1">
              ambient
              <input
                type="number"
                min="0"
                step="0.05"
                value={particleAmbientLight}
                oninput={(e) =>
                  onParticleLighting(particleDirectLight, Number(e.currentTarget.value))}
                class="border-border bg-background w-14 rounded border px-1 py-0.5 text-right"
                aria-label="Particle ambient lighting factor"
              />
            </label>
          </div>
        </section>

        <!-- Intensity channels (gameplay input, not authored data) -->
        {#if (activeRecord.intensityChannels ?? []).length > 0}
          <section class="border-border bg-muted/20 mb-3 rounded border p-2">
            <div class="mb-1.5 flex items-center justify-between">
              <span class="text-muted-foreground text-[9px] uppercase tracking-wider">
                intensity
              </span>
              <span class="text-muted-foreground text-[9px]">{intensityPreset}</span>
            </div>
            <div class="mb-2 flex flex-wrap items-center gap-1.5 font-mono text-[10px]">
              <button
                type="button"
                class="rounded border px-1.5 py-0.5 {intensityPreset === 'authored'
                  ? 'border-primary/60 bg-primary/15 text-foreground'
                  : 'border-border text-muted-foreground hover:text-foreground'}"
                onclick={setPresetAuthored}
                title="Authored per-channel defaults (record.intensityChannels[].defaultIntensity)"
              >
                authored
              </button>
              <button
                type="button"
                class="rounded border px-1.5 py-0.5 {intensityPreset === 'caliber'
                  ? 'border-primary/60 bg-primary/15 text-foreground'
                  : 'border-border text-muted-foreground hover:text-foreground'}"
                onclick={setPresetCaliber}
                title="Gun caliber × barrel-count factor × effect modifier, clamped to [0, 10]"
              >
                gun shot
              </button>
              <input
                type="number"
                min="50"
                max="550"
                step="1"
                value={caliberMm}
                oninput={(e) => onCaliberInput(Number(e.currentTarget.value))}
                class="border-border bg-background w-14 rounded border px-1 py-0.5 text-right tabular-nums"
                aria-label="Gun caliber in millimetres"
              />
              <span class="text-muted-foreground">
                mm → {caliberLawIntensity(caliberMm).toFixed(1)}
              </span>
            </div>
            <div class="mb-2 flex flex-wrap gap-2 font-mono text-[10px]">
              <label class="flex items-center gap-1">
                barrels
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={barrelCount}
                  oninput={(e) => onShotParameters(Number(e.currentTarget.value), shotModifier)}
                  class="border-border bg-background w-12 rounded border px-1 py-0.5 text-right"
                  aria-label="Gun mount barrel count"
                />
              </label>
              <label class="flex items-center gap-1">
                modifier
                <input
                  type="number"
                  min="0"
                  step="0.05"
                  value={shotModifier}
                  oninput={(e) => onShotParameters(barrelCount, Number(e.currentTarget.value))}
                  class="border-border bg-background w-14 rounded border px-1 py-0.5 text-right"
                  aria-label="Gun effect intensity modifier"
                />
              </label>
            </div>
            <p class="text-muted-foreground mb-2 text-[10px]">
              Single-emitter preview; barrel count controls shot intensity.
            </p>
            <div class="flex flex-col gap-1 font-mono text-[10px]">
              {#each activeRecord.intensityChannels ?? [] as ch, i (i)}
                <label class="flex items-center gap-2">
                  <span class="text-muted-foreground w-20 flex-none truncate" title={ch.name}>
                    {ch.name}
                  </span>
                  <input
                    type="range"
                    min={ch.minIntensity ?? 0}
                    max={ch.maxIntensity ?? 10}
                    step="0.1"
                    value={intensityValues[i] ?? 1}
                    oninput={(e) => onChannelSlider(i, Number(e.currentTarget.value))}
                    class="min-w-0 flex-1"
                  />
                  <span class="text-foreground w-8 flex-none text-right tabular-nums">
                    {(intensityValues[i] ?? 1).toFixed(1)}
                  </span>
                </label>
              {/each}
            </div>
          </section>
        {/if}

        <!-- Texture preview -->
        {#if firstTexture?.url}
          <section class="mb-3 flex items-start gap-3">
            <DdsTexturePreview
              paths={[firstTexture.url]}
              baseUrl={repoBase}
              slot="baseColor"
              size={96}
            />
            <div class="flex min-w-0 flex-1 flex-col gap-0.5">
              <div class="text-muted-foreground text-[9px] uppercase tracking-wider">
                sprite map
              </div>
              <code class="text-foreground font-mono text-[10px] break-all">
                {firstTexture.name}
              </code>
              <div class="text-muted-foreground text-[9px]">
                {textureCount.stamped} stamped · {textureCount.missing} missing
              </div>
            </div>
          </section>
        {:else if textureCount.missing > 0}
          <section class="mb-3 rounded border border-border bg-muted/20 p-2">
            <div class="text-muted-foreground text-[10px]">
              textures referenced but not extracted yet — render falls back to procedural disc.
              Scaffold a ship that uses this particle to populate the cache.
            </div>
          </section>
        {/if}

        <!-- Per-system breakdown -->
        <section class="mb-3 flex flex-col gap-2">
          <div class="text-muted-foreground text-[9px] uppercase tracking-wider">systems</div>
          {#each activeRecord.systems ?? [] as s, si (si)}
            {@const checklist = actionChecklist(s)}
            <details
              class="border-border bg-popover/40 rounded border px-2 py-1.5 [&[open]]:bg-popover/70"
              open={si === 0}
            >
              <summary
                class="text-foreground cursor-pointer text-[11px] font-mono hover:text-primary"
              >
                #{si} · cap {s.general?.capacity ?? '—'} · maxAge {(
                  s.general?.maxParticleAge ?? 0
                ).toFixed(2)}s · {checklist.length} comps
              </summary>
              <div class="mt-2 flex flex-col gap-2">
                <!-- Emitter sub-fields -->
                <div>
                  <div class="text-muted-foreground text-[9px] uppercase tracking-wider">
                    emitter
                  </div>
                  <pre
                    class="text-foreground bg-background/40 mt-1 max-h-32 overflow-auto rounded border border-border/60 p-1 font-mono text-[10px]">{JSON.stringify(
                      s.emitter ?? {},
                      null,
                      2,
                    )}</pre>
                </div>
                <!-- Renderer + animation -->
                <div>
                  <div class="text-muted-foreground text-[9px] uppercase tracking-wider">
                    renderer + animation
                  </div>
                  <pre
                    class="text-foreground bg-background/40 mt-1 max-h-32 overflow-auto rounded border border-border/60 p-1 font-mono text-[10px]">{JSON.stringify(
                      { renderer: s.renderer, animation: s.animation },
                      null,
                      2,
                    )}</pre>
                </div>
                <!-- Component action checklist -->
                <div>
                  <div class="text-muted-foreground text-[9px] uppercase tracking-wider">
                    components · {checklist.filter((x) => x.rendered).length}/{checklist.length} rendered
                  </div>
                  <ul class="mt-1 flex flex-col gap-0.5">
                    {#each checklist as item, ci (ci)}
                      <li class="flex items-center gap-2 font-mono text-[10px]">
                        <span
                          class="inline-flex size-2 flex-none rounded-full {item.rendered
                            ? 'bg-emerald-500'
                            : 'bg-amber-500/60'}"
                          title={item.rendered ? 'rendered' : 'ignored by current renderer'}
                        ></span>
                        <span class="text-foreground">{item.action}</span>
                        <span class="text-muted-foreground text-[9px]">({item.kind})</span>
                      </li>
                    {/each}
                  </ul>
                </div>
                <!-- Raw component bodies -->
                <details class="text-[10px]">
                  <summary class="text-muted-foreground cursor-pointer hover:text-foreground">
                    raw component bodies
                  </summary>
                  <pre
                    class="text-foreground bg-background/40 mt-1 max-h-64 overflow-auto rounded border border-border/60 p-1 font-mono text-[10px]">{JSON.stringify(
                      s.components ?? [],
                      null,
                      2,
                    )}</pre>
                </details>
              </div>
            </details>
          {/each}
        </section>

        <!-- Full raw JSON for power users -->
        <details class="text-[10px]">
          <summary class="text-muted-foreground cursor-pointer hover:text-foreground">
            full parsed record (JSON)
          </summary>
          <pre
            class="text-foreground bg-background/40 mt-1 max-h-96 overflow-auto rounded border border-border/60 p-1 font-mono text-[10px]">{JSON.stringify(
              activeRecord,
              null,
              2,
            )}</pre>
        </details>
      {/if}
    </div>
  </aside>
</div>
