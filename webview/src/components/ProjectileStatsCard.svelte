<script lang="ts">
  // Per-projectile stats card. Renders the ammo profile's `visual` +
  // `effects` blocks as a key-value table, plus a top metadata row
  // (ammo_id / type / species / linked mesh).
  //
  // The two blocks have shape-varying contents per species (artillery
  // shells carry tracer color + size_mod; torpedoes carry speed/range;
  // bombs carry parachute/drag; lasers carry colour/intensity). We
  // render generically: pretty-print primitives + arrays inline, format
  // numbers, and nest objects one level deep.

  import type { AmmoProfile } from '$lib/types/projectiles';

  interface Props {
    /** Selected ammo id (key in ammo_profiles.profiles). Null when
     *  viewing a mesh row that has no specific ammo selected. */
    ammoId: string | null;
    /** The full profile if ammoId is set. Null otherwise. */
    profile: AmmoProfile | null;
    /** Optional: title shown when no ammo is selected. */
    fallbackTitle?: string;
  }

  const { ammoId, profile, fallbackTitle }: Props = $props();

  /** Render a JS value to a short display string. Primitives → text;
   *  arrays of numbers → "[1.00, 2.00, 0.50]"; objects → JSON one-liner;
   *  everything else → JSON.stringify with a 60-char cap. */
  function fmtValue(v: unknown): string {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number') {
      // Show ints exactly; decimals capped to 3 places to keep rows tidy.
      if (Number.isInteger(v)) return String(v);
      return v.toFixed(3).replace(/\.?0+$/, '') || '0';
    }
    if (typeof v === 'string') return v;
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (Array.isArray(v)) {
      return '[' + v.map((x) => fmtValue(x)).join(', ') + ']';
    }
    if (typeof v === 'object') {
      const s = JSON.stringify(v);
      return s.length > 60 ? s.slice(0, 57) + '…' : s;
    }
    return String(v);
  }

  /** Render a tracer / colour-typed array as a CSS color swatch + label.
   *  Used when a value looks like an RGB triple in 0..1 range. */
  function looksLikeColor(v: unknown): v is [number, number, number] {
    return (
      Array.isArray(v) &&
      v.length === 3 &&
      v.every(
        (x) => typeof x === 'number' && Number.isFinite(x) && x >= 0 && x <= 1,
      )
    );
  }

  function cssColor(rgb: [number, number, number]): string {
    const [r, g, b] = rgb.map((x) => Math.round(x * 255));
    return `rgb(${r}, ${g}, ${b})`;
  }

  /** Sort entries by key for stable display — the producer emits in
   *  insertion order which can shuffle between rebuilds. */
  function entries(o: Record<string, unknown> | undefined): [string, unknown][] {
    if (!o) return [];
    return Object.entries(o).sort(([a], [b]) => a.localeCompare(b));
  }

  const visualEntries = $derived(entries(profile?.visual));
  const effectsEntries = $derived(entries(profile?.effects));
</script>

<aside class="bg-popover/40 flex flex-col gap-2 px-3 py-3 text-xs">
  {#if profile && ammoId}
    <div class="flex flex-col gap-0.5">
      <code class="text-foreground font-mono text-[11px] break-all">{ammoId}</code>
      <div class="text-muted-foreground flex flex-wrap gap-x-3 text-[11px]">
        <span><strong class="text-foreground">{profile.ammo_type}</strong></span>
        <span>species: <code>{profile.species}</code></span>
        {#if profile.asset_id}
          <span>mesh: <code>{profile.asset_id}</code></span>
        {:else}
          <span class="text-amber-400">no mesh (pure VFX)</span>
        {/if}
      </div>
    </div>

    {#if visualEntries.length > 0}
      <div class="flex flex-col gap-0.5">
        <div class="text-muted-foreground text-[10px] uppercase tracking-wider font-semibold">
          Visual
        </div>
        <dl class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
          {#each visualEntries as [k, v] (k)}
            <dt class="text-muted-foreground font-mono">{k}</dt>
            <dd class="m-0 font-mono break-all">
              {#if looksLikeColor(v)}
                <span
                  class="inline-block h-2.5 w-2.5 rounded-sm align-middle mr-1.5 border border-white/20"
                  style="background: {cssColor(v)}"
                ></span>
                {fmtValue(v)}
              {:else}
                {fmtValue(v)}
              {/if}
            </dd>
          {/each}
        </dl>
      </div>
    {/if}

    {#if effectsEntries.length > 0}
      <div class="flex flex-col gap-0.5">
        <div class="text-muted-foreground text-[10px] uppercase tracking-wider font-semibold">
          Effects
        </div>
        <dl class="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
          {#each effectsEntries as [k, v] (k)}
            <dt class="text-muted-foreground font-mono">{k}</dt>
            <dd class="m-0 font-mono break-all">{fmtValue(v)}</dd>
          {/each}
        </dl>
      </div>
    {/if}

    {#if visualEntries.length === 0 && effectsEntries.length === 0}
      <p class="text-muted-foreground m-0 text-[11px]">
        No visual / effects data for this profile.
      </p>
    {/if}
  {:else if fallbackTitle}
    <code class="text-foreground font-mono text-[11px] break-all">{fallbackTitle}</code>
    <p class="text-muted-foreground m-0 text-[11px]">
      Pick an ammo profile from the list to see its stats.
    </p>
  {:else}
    <p class="text-muted-foreground m-0 text-[11px]">
      Select an entry from the list.
    </p>
  {/if}
</aside>
