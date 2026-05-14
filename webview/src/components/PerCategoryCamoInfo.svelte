<script lang="ts">
  // Per-category camo info card. Surfaces every permoflage on the
  // selected Vehicle whose camouflages.xml entry sets per-part textures —
  // the strict mat_camo hybrid case (mat_palette atlas mixed with masks),
  // the non-mat hull_palette case (per-part masks + palette, e.g.
  // Bismarck's camo_permanent_1), and tile_broadcast (single mask shared
  // across every category).
  //
  // Each row carries a colour swatch sampled via sampleDxtFirstBlockColor
  // — works for BC1/2/3; BC7 falls through to a striped failure pattern
  // but the filename + categories stay visible. See memory
  // `project_mat_camo_hybrid_shipped.md`.

  import { sampleDxtFirstBlockColor } from '$lib/dds';
  import { repoUrl } from '$lib/api';
  import { TOPOLOGY_LABELS } from '$lib/extract/labels';
  import type { Permoflage, Topology } from '$lib/types/extract';

  interface Props {
    permoflages: Permoflage[];
  }

  const { permoflages }: Props = $props();

  const PER_CAT_TOPOS: ReadonlySet<Topology> = new Set(['mat_palette', 'hull_palette', 'tile_broadcast']);
  const perCatPerms = $derived(permoflages.filter((p) => PER_CAT_TOPOS.has(p.topology)));

  const CAT_ORDER = ['gun', 'director', 'rangefinder', 'float', 'plane', 'misc', 'bulge', 'deckhouse', 'tile'];

  // Same swatch cache shape as the ship-page Camos panel. Keyed by full
  // /repo/* URL so two permoflages pointing at the same DDS share one
  // decode + paint.
  let swatchCache = $state<Map<string, string>>(new Map());
  const inflight = new Set<string>();
  let failed = $state<Set<string>>(new Set());

  type Row = { libPath: string; url: string; stem: string; kind: 'atlas' | 'mask'; cats: string[] };

  function collapseRows(p: Permoflage): Row[] {
    const tex = p.category_textures ?? {};
    const byPath = new Map<string, { cats: string[]; kind: 'atlas' | 'mask' }>();
    for (const [cat, entry] of Object.entries(tex)) {
      const slot = byPath.get(entry.lib_path) ?? { cats: [], kind: entry.kind };
      slot.cats.push(cat);
      byPath.set(entry.lib_path, slot);
    }
    return Array.from(byPath.entries()).map(([libPath, slot]) => {
      slot.cats.sort(
        (a, b) => (CAT_ORDER.indexOf(a) + 1 || 99) - (CAT_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b),
      );
      const stem = libPath.split(/[\\/]/).pop() ?? '';
      return { libPath, url: repoUrl(libPath), stem, kind: slot.kind, cats: slot.cats };
    });
  }

  const rendered = $derived(
    perCatPerms.map((p) => ({
      p,
      rows: collapseRows(p),
    })),
  );

  // Async-decode + paint each swatch. Idempotent — concurrent renders
  // share the same in-flight Promise via `inflight` so the same URL
  // doesn't fan out duplicate decodes.
  function decodeRow(url: string) {
    if (swatchCache.has(url) || failed.has(url) || inflight.has(url)) return;
    inflight.add(url);
    void sampleDxtFirstBlockColor(url)
      .then((rgb) => {
        inflight.delete(url);
        if (rgb) {
          const next = new Map(swatchCache);
          next.set(url, rgb);
          swatchCache = next;
        } else {
          const next = new Set(failed);
          next.add(url);
          failed = next;
        }
      })
      .catch(() => {
        inflight.delete(url);
        const next = new Set(failed);
        next.add(url);
        failed = next;
      });
  }

  // Kick off decodes whenever the rendered list changes — re-renders are
  // cheap because cache hits short-circuit instantly.
  $effect(() => {
    for (const entry of rendered) {
      for (const r of entry.rows) decodeRow(r.url);
    }
  });

  function matCount(): number {
    return perCatPerms.filter((p) => p.topology === 'mat_palette').length;
  }
</script>

{#if perCatPerms.length > 0}
  <section
    class="border-sky-500/40 bg-sky-950/30 my-3 rounded border px-3.5 py-2.5 text-xs"
  >
    <div class="text-sky-200">
      <strong>ℹ Per-category camo:</strong>
      {#if matCount() > 0}
        {perCatPerms.length} of {permoflages.length} permoflage(s) paint per category —
        {matCount()} include <code>libraries/camo_mat/</code> atlas overlays (mat_camo hybrid),
        the rest are palette-composited masks only.
      {:else}
        {perCatPerms.length} of {permoflages.length} permoflage(s) paint per category. None mix in
        <code>libraries/camo_mat/</code> atlases on this Vehicle — every entry below is mask + palette.
      {/if}
    </div>
    <ul class="mt-2.5 flex flex-col gap-2.5 list-none p-0">
      {#each rendered as entry (entry.p.exterior_id)}
        <li class="border-border bg-card/60 rounded border px-2.5 py-2">
          <div class="flex flex-wrap items-center gap-1.5">
            <strong class="text-foreground">{entry.p.display_name}</strong>
            {#if entry.p.camouflage}
              <code class="font-mono text-[10px]" title="WG camouflage entry">{entry.p.camouflage}</code>
            {:else}
              <span class="text-muted-foreground text-[10px]">(no camo entry)</span>
            {/if}
            <span class="bg-amber-900/40 rounded px-1.5 py-[1px] text-[10px] text-amber-200">
              {TOPOLOGY_LABELS[entry.p.topology]}
            </span>
            <code class="text-muted-foreground font-mono text-[10px]">{entry.p.exterior_id}</code>
          </div>
          {#if entry.rows.length > 0}
            <div class="mt-1.5 flex flex-col gap-1">
              {#each entry.rows as r (r.libPath)}
                {@const bg = swatchCache.get(r.url) ?? 'rgba(255,255,255,0.05)'}
                <div class="flex flex-wrap items-center gap-1.5 text-[11px]">
                  <span
                    title="{r.stem} (BC1 first-block sample; BC7 shows stripes){failed.has(r.url) ? ' · sample failed' : ''}"
                    class="border-border inline-block size-3 flex-none rounded-sm border {failed.has(r.url)
                      ? 'opacity-50'
                      : ''}"
                    style="background: {bg}"
                  ></span>
                  <span
                    class="rounded px-1.5 py-[1px] text-[10px] {r.kind === 'atlas'
                      ? 'bg-amber-900/40 text-amber-200'
                      : 'bg-cyan-900/40 text-cyan-200'}"
                    title={r.kind === 'atlas'
                      ? 'Flat /mat_camo/ atlas — overlay multiplied over base albedo'
                      : 'Per-zone R/G/B mask — palette-composited per colorScheme'}>{r.kind}</span
                  >
                  <code class="text-muted-foreground font-mono text-[10px]" title={r.libPath}>{r.stem}</code>
                  <span class="ml-auto flex flex-wrap gap-1">
                    {#each r.cats as c (c)}
                      <code class="bg-secondary text-muted-foreground rounded px-1 py-[1px] font-mono text-[10px]">{c}</code>
                    {/each}
                  </span>
                </div>
              {/each}
            </div>
          {:else}
            <div class="text-muted-foreground mt-1.5 text-[11px]">(no per-category textures resolved)</div>
          {/if}
        </li>
      {/each}
    </ul>
  </section>
{/if}
