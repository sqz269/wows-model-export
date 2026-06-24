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

  import { onMount } from 'svelte';
  import * as THREE from 'three';
  import Search from '@lucide/svelte/icons/search';
  import Play from '@lucide/svelte/icons/play';
  import Pause from '@lucide/svelte/icons/pause';
  import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
  import XIcon from '@lucide/svelte/icons/x';
  import { Button } from '$lib/components/ui/button';
  import { ApiError, fetchJson } from '$lib/api';
  import { navigate } from '$lib/router';
  import { navState } from '$lib/nav_state.svelte';
  import { hasModifier, isTypingContext } from '$lib/shortcuts';
  import { ParticleScene, type ParticleAttachmentHandle } from '$lib/three/particles';
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
  let elapsedS = $state(0);
  let playing = $state(true);

  // Per-system bounding-box overlay (inspector diagnostic — wraps each
  // system's live particles in a coloured AABB).
  let showBounds = $state(false);
  /** Indices of systems toggled OFF (hidden) in the inspector. */
  let hiddenSystems = $state<Set<number>>(new Set());
  /** Per-system panel rows (rebuilt each frame: live count + hidden flag). */
  let systemRows = $state<
    Array<{ i: number; name: string; alive: number; hidden: boolean }>
  >([]);

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

  function mountViewer(container: HTMLElement) {
    // Compact-ish scene: camera close to the origin, smaller grid, no
    // axes hint (the inspector doesn't care about world axes — the
    // emitter is at (0,0,0) by construction).
    env = createSceneEnvironment(container, {
      background: 0x0a0c11,
      cameraPosition: [12, 8, 12],
      gridSize: 40,
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
    scene.setSortCamera(env.camera);
    const sun = env.getSunLight();
    scene.setSunLighting(sun.direction, sun.color);
    env.scene.add(scene.root);

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
      scene!.tick();
      syncSystems();
      env!.render();
      // Mirror the simulator's alive count + elapsed time into reactive
      // state so the overlay readouts update without a manual ping.
      if (currentHandle) {
        let total = 0;
        let maxElapsed = 0;
        for (const s of currentHandle.systems) {
          total += s.aliveCount;
          if (s.elapsedSeconds > maxElapsed) maxElapsed = s.elapsedSeconds;
        }
        aliveCount = total;
        elapsedS = maxElapsed;
      } else {
        aliveCount = 0;
        elapsedS = 0;
      }
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
      systems[i].points.visible = !hiddenSystems.has(i);
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
    currentHandle = null;
    hiddenSystems = new Set();
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
    );
    if (handles.length === 0) return;
    const h = handles[0];
    scene.setAttachmentActive(h, playing);
    currentHandle = h;
  }

  // Rebuild whenever the active record changes.
  $effect(() => {
    rebuildSceneFromRecord(activeRecord);
  });

  // Toggle live activity when `playing` changes (without rebuilding the
  // simulator — preserves the ring buffer state).
  $effect(() => {
    if (currentHandle && scene) {
      scene.setAttachmentActive(currentHandle, playing);
    }
  });

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

  /** Set of action names the WEBVIEW emitter (`particles.ts`) actually
   *  consumes today. Anything else in a record is silently ignored —
   *  the inspector flags these so the user knows render fidelity is
   *  limited there.
   *
   *  Source: `webview/src/lib/three/particles.ts` — the
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
   *  (`three/particles.ts` `blendConfigForPsRbt` + the flipbook-UV shader
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
    for (const s of rec.systems ?? []) {
      const r = s.renderer;
      if (!r) continue;
      if (r.blendType && PLACEHOLDER_BLEND_MODES.has(r.blendType)) {
        placeholderBlends.add(r.blendType);
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
    if (placeholderBlends.size > 0) {
      gaps.push(
        `blend mode ${[...placeholderBlends].join(' / ')} approximated as ` +
          'screen-space refraction from a scene-color copy; native water/heat-haze pass not exact',
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
  <section class="relative flex flex-1 min-w-0 flex-col">
    <!-- Toolbar -->
    <div class="bg-card border-border flex flex-none items-center gap-2 border-b px-4 py-2 text-xs">
      {#if selectedPath}
        <code class="text-foreground font-mono">{shortName(selectedPath)}</code>
        <span class="text-muted-foreground/60">·</span>
        <code class="text-muted-foreground font-mono text-[10px]">{selectedPath}</code>
      {:else}
        <span class="text-muted-foreground">no particle selected</span>
      {/if}

      <div class="ml-auto flex items-center gap-2">
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
        <Button
          variant="outline"
          size="sm"
          onclick={() => (playing = !playing)}
          title={playing ? 'Pause (space)' : 'Play (space)'}
        >
          {#if playing}
            <Pause class="size-3" />
          {:else}
            <Play class="size-3" />
          {/if}
        </Button>
        <Button variant="outline" size="sm" onclick={restart} title="Restart simulation (R)">
          <RotateCcw class="size-3" />
        </Button>
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
            t <span class="text-foreground tabular-nums">{elapsedS.toFixed(2)}s</span>
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
