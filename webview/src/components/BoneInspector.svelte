<script lang="ts">
  // Bone inspector — lists every named node in the loaded accessory GLB
  // with its bind-pose world position, and exposes per-bone X/Y/Z Euler
  // rotation sliders. Diagnostic tool for verifying where a pivot sits
  // relative to the geometry it influences, and for seeing how an
  // arbitrary bone deforms the mesh — distinct from the higher-level
  // yaw/pitch aim used by the ship view, which only drives `Rotate_Y`
  // / `Rotate_X`.
  //
  // The viewer captures bone positions at GLB load time (snapshot),
  // and rotation is applied as `bone.quaternion = rest × Euler(x,y,z)`
  // so dragging a slider doesn't accumulate. First touch of a bone
  // snaps the rest quaternion; `resetBone(name)` discards the snap.

  import { untrack } from 'svelte';
  import type { AccessoryViewer, BoneInfo } from '$lib/accessory';

  interface Props {
    /** Live viewer handle. May be `null` briefly while the viewer
     *  remounts on asset switch; the component renders a placeholder. */
    viewer: AccessoryViewer | null;
    /** Asset id (used as a re-fetch trigger when the user switches
     *  between assets without unmounting this component). */
    assetId: string;
    /** Bumped by the parent on each successful GLB load — the bones
     *  list is captured during the load, so we re-fetch when this
     *  changes to pick up the freshly-populated list. Pass any value
     *  whose identity changes per load (e.g. `result?.bounds`). */
    loadToken?: unknown;
  }

  const { viewer, assetId, loadToken }: Props = $props();

  // Bones snapshot — pulled from the viewer on every asset switch.
  let bones = $state<BoneInfo[]>([]);

  // Per-bone Euler rotation in DEGREES (user-friendly; converted to
  // radians before pushing to the viewer). Map by bone name so adding
  // a new bone or switching assets doesn't drop other bones' state.
  let rotDeg = $state<Map<string, { x: number; y: number; z: number }>>(new Map());

  let highlighted = $state<string | null>(null);

  // Name-filter input. Substring match, case-insensitive. Empty shows
  // all bones. The filter is sticky across asset switches because the
  // same name pattern (e.g. 'BlendBone') typically applies everywhere.
  let filter = $state('');

  $effect(() => {
    const v = viewer;
    // Re-fetch on asset switch OR on load completion. The viewer captures
    // bones inside loadGlb; the parent's `result` changes identity once
    // that resolves, so threading it in as `loadToken` gives us a clean
    // signal without polling.
    void assetId;
    void loadToken;
    untrack(() => {
      if (!v) {
        bones = [];
        rotDeg = new Map();
        highlighted = null;
        return;
      }
      bones = v.getBones();
      // Clear per-bone state only on asset switch — keep current values
      // if this re-fetch was just because of a same-asset reload (rare).
      rotDeg = new Map();
      highlighted = null;
    });
  });

  function getRot(name: string): { x: number; y: number; z: number } {
    return rotDeg.get(name) ?? { x: 0, y: 0, z: 0 };
  }

  function pushRotation(name: string, r: { x: number; y: number; z: number }) {
    rotDeg.set(name, r);
    rotDeg = new Map(rotDeg); // trigger Svelte reactivity
    viewer?.setBoneEuler(name, deg2rad(r.x), deg2rad(r.y), deg2rad(r.z));
  }

  function setAxis(name: string, axis: 'x' | 'y' | 'z', deg: number) {
    const r = { ...getRot(name) };
    r[axis] = deg;
    pushRotation(name, r);
  }

  function resetOne(name: string) {
    rotDeg.delete(name);
    rotDeg = new Map(rotDeg);
    viewer?.resetBone(name);
  }

  function resetAll() {
    rotDeg = new Map();
    viewer?.resetAllBones();
  }

  function toggleHighlight(name: string) {
    if (highlighted === name) {
      highlighted = null;
      viewer?.highlightBone(null);
    } else {
      highlighted = name;
      viewer?.highlightBone(name);
    }
  }

  function deg2rad(d: number): number {
    return (d * Math.PI) / 180;
  }

  function fmtPos(n: number): string {
    return (n >= 0 ? '+' : '') + n.toFixed(3);
  }

  const filtered = $derived.by(() => {
    if (!filter.trim()) return bones;
    const needle = filter.trim().toLowerCase();
    return bones.filter((b) => b.name.toLowerCase().includes(needle));
  });

  // Count of bones the user has actually rotated — drives "reset all"
  // visibility.
  const touchedCount = $derived(rotDeg.size);
</script>

<div class="flex flex-col gap-2">
  <!-- Toolbar: bone count, filter, reset-all. -->
  <div class="flex items-center gap-3 text-[11px]">
    <span class="text-muted-foreground tabular-nums">
      {filtered.length}/{bones.length} bone{bones.length === 1 ? '' : 's'}
    </span>
    <input
      type="text"
      placeholder="filter by name (e.g. Rotate_X, BlendBone)"
      bind:value={filter}
      class="h-6 flex-1 max-w-[280px] rounded border border-border bg-popover px-1.5 text-[11px] text-foreground focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring"
    />
    {#if touchedCount > 0}
      <button
        type="button"
        onclick={resetAll}
        class="rounded border border-border bg-popover hover:bg-accent px-2 py-0.5 text-[11px] text-foreground"
        title="Restore every bone to its rest pose"
      >
        Reset all ({touchedCount})
      </button>
    {/if}
  </div>

  {#if bones.length === 0}
    <div class="text-muted-foreground text-[11px]">
      No named nodes in this GLB (asset may not be loaded yet, or has no rig).
    </div>
  {:else}
    <!-- Bone list. Two-line layout per bone: header row with name + pos
         + actions, then a sub-row of three slider columns. -->
    <div class="flex flex-col gap-1.5">
      {#each filtered as b (b.name)}
        {@const r = getRot(b.name)}
        {@const isTouched = rotDeg.has(b.name)}
        {@const isHighlighted = highlighted === b.name}
        <div
          class="border-b border-border/40 pb-1.5 last:border-b-0 last:pb-0"
          class:bg-accent={isHighlighted}
        >
          <!-- Header row -->
          <div class="flex items-baseline gap-3 text-[11px]">
            <code class="font-mono text-foreground" class:font-bold={isTouched}>{b.name}</code>
            <span class="text-muted-foreground tabular-nums">
              ({fmtPos(b.worldPosition.x)}, {fmtPos(b.worldPosition.y)}, {fmtPos(b.worldPosition.z)})
            </span>
            <span class="text-muted-foreground/60 text-[10px]">
              {b.isSkinJoint ? 'joint' : 'pivot'}
              {b.hasChildren ? '' : '·leaf'}
            </span>
            <span class="flex-1"></span>
            <button
              type="button"
              onclick={() => toggleHighlight(b.name)}
              class="rounded border border-border bg-popover hover:bg-accent px-1.5 py-0.5 text-[10px]"
              class:border-primary={isHighlighted}
              title="Pin a marker sphere at the bind-pose position"
            >
              {isHighlighted ? '● pinned' : '○ pin'}
            </button>
            {#if isTouched}
              <button
                type="button"
                onclick={() => resetOne(b.name)}
                class="rounded border border-border bg-popover hover:bg-accent px-1.5 py-0.5 text-[10px]"
                title="Restore to rest pose"
              >
                ↻ reset
              </button>
            {/if}
          </div>
          <!-- Slider row: X / Y / Z Euler (degrees, applied as
               rest × R(x,y,z) on the bone's local quaternion). -->
          <div class="flex flex-wrap items-center gap-3 mt-0.5 text-[10px]">
            {#each (['x', 'y', 'z'] as const) as axis (axis)}
              <label class="flex items-center gap-1.5">
                <span class="w-3 text-muted-foreground uppercase">{axis}</span>
                <input
                  type="range"
                  min="-180"
                  max="180"
                  step="1"
                  value={r[axis]}
                  oninput={(e) => setAxis(b.name, axis, Number((e.currentTarget as HTMLInputElement).value))}
                  class="w-32 accent-primary"
                />
                <span class="w-10 text-right tabular-nums text-foreground">{r[axis].toFixed(0)}°</span>
              </label>
            {/each}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>
