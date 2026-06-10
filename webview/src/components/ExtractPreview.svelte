<script lang="ts">
  // Right-column preview pane for a selected (Vehicle, Permoflage) pair.
  // Headers + collision/VFS warnings + per-category camo info + summary
  // table + permoflage routing block.
  //
  // The sub-panels (run options + skin pack form + job tail) are sibling
  // components composed by the parent route — keeping the data flow flat
  // means a single source of truth for run state, even though the panels
  // appear stacked vertically inside the same scroll container.

  import { TOPOLOGY_LABELS, VFS_STATUS_META } from '$lib/extract/labels';
  import PerCategoryCamoInfo from './PerCategoryCamoInfo.svelte';
  import type { Permoflage, Vehicle } from '$lib/types/extract';

  interface Props {
    vehicle: Vehicle;
    permoflage: Permoflage | null;
    allPermoflages: Permoflage[];
  }

  const { vehicle, permoflage, allPermoflages }: Props = $props();

  const isMeshSwap = $derived(permoflage?.topology === 'mesh_swap');
  const flags = $derived(
    [
      vehicle.is_premium ? 'premium' : '',
      vehicle.is_in_test ? 'in_test' : '',
      vehicle.is_paper ? 'paper' : '',
    ]
      .filter(Boolean)
      .join(', ') || '—',
  );

  const vfsBadge = $derived.by(() => {
    const vs = vehicle.vfs_status;
    if (!vs || vs === 'ok' || vs === 'unknown') return null;
    return { vs, ...VFS_STATUS_META[vs] };
  });
</script>

<header class="border-border bg-card border-b px-4 py-3">
  <h2 class="text-foreground m-0 text-base font-semibold">{vehicle.display_name}</h2>
  <div class="text-muted-foreground mt-1 text-[11px]">
    <code class="font-mono">{vehicle.top_key}</code> · model_dir
    <code class="font-mono">{vehicle.model_dir || '?'}</code>
  </div>
</header>

<div class="px-4 py-3">
  {#if vehicle.shares_model_dir_with.length > 0}
    <div class="border-amber-500/40 bg-amber-950/30 mb-3 rounded border px-3 py-2 text-xs text-amber-200">
      <strong>⚠ model_dir collision.</strong>
      <code class="font-mono">{vehicle.model_dir || '?'}</code> is also used by
      {#each vehicle.shares_model_dir_with as k, i (k)}
        <code class="font-mono">{k}</code>{#if i < vehicle.shares_model_dir_with.length - 1},
        {/if}
      {/each}.
      The extract pins
      <code class="font-mono">--gameparams-ship-id {vehicle.top_key || vehicle.param_index}</code>
      so this Vehicle's stats end up in the sidecar (ballistics / tier / permoflage list / armor zones).
    </div>
  {/if}

  {#if vfsBadge}
    {@const flavour =
      vfsBadge.vs === 'no_splash'
        ? "The hull GLB will export, but with no hitbox / damage-zone data — consumers will see the model but raycasts won't resolve damage."
        : 'Extraction will fail at the toolkit step.'}
    <div
      class="mb-3 rounded border px-3 py-2 text-xs {vfsBadge.sev === 'warn'
        ? 'border-amber-500/40 bg-amber-950/30 text-amber-200'
        : 'border-rose-500/40 bg-rose-950/30 text-rose-200'}"
    >
      <strong>⚠ VFS: {vfsBadge.label}.</strong>
      {vfsBadge.title} {flavour}
    </div>
  {/if}

  <PerCategoryCamoInfo permoflages={allPermoflages} />

  <section class="border-border border-t pt-3">
    <h3 class="text-foreground mb-2 text-xs font-semibold uppercase tracking-wide">Vehicle</h3>
    <table class="w-full text-xs">
      <tbody>
        <tr class="border-border/40 border-b">
          <th class="text-muted-foreground py-1 pr-3 text-left font-medium">param_index</th>
          <td class="py-1"><code class="font-mono">{vehicle.param_index}</code></td>
        </tr>
        <tr class="border-border/40 border-b">
          <th class="text-muted-foreground py-1 pr-3 text-left font-medium">top_key</th>
          <td class="py-1"><code class="font-mono">{vehicle.top_key}</code></td>
        </tr>
        <tr class="border-border/40 border-b">
          <th class="text-muted-foreground py-1 pr-3 text-left font-medium">nation / class / tier</th>
          <td class="py-1">
            {vehicle.nation || '?'} · {vehicle.class || '?'} · T{vehicle.tier ?? '?'}
          </td>
        </tr>
        <tr class="border-border/40 border-b">
          <th class="text-muted-foreground py-1 pr-3 text-left font-medium">flags</th>
          <td class="py-1">{flags}</td>
        </tr>
        <tr class="border-border/40 border-b">
          <th class="text-muted-foreground py-1 pr-3 text-left font-medium">native permoflage</th>
          <td class="py-1">
            {#if vehicle.native_permoflage}
              <code class="font-mono">{vehicle.native_permoflage}</code>
            {:else}
              —
            {/if}
          </td>
        </tr>
        <tr class="border-border/40 border-b">
          <th class="text-muted-foreground py-1 pr-3 text-left font-medium">permoflage count</th>
          <td class="py-1">{vehicle.permoflages_count}</td>
        </tr>
        {#if vehicle.shares_model_dir_with.length > 0}
          <tr>
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium align-top">shares model_dir with</th>
            <td class="py-1">
              {#each vehicle.shares_model_dir_with as k, i (k)}
                <code class="font-mono">{k}</code>{#if i < vehicle.shares_model_dir_with.length - 1},
                {/if}
              {/each}
            </td>
          </tr>
        {/if}
      </tbody>
    </table>
  </section>

  <section class="border-border border-t mt-3 pt-3">
    <h3 class="text-foreground mb-2 text-xs font-semibold uppercase tracking-wide">Permoflage routing</h3>
    {#if !permoflage}
      <div class="text-foreground text-xs">
        No permoflage picked → <code class="font-mono">--variant-permoflage none</code>
        (force base hull, no native-permoflage routing).
      </div>
    {:else if isMeshSwap}
      {@const variantTag = permoflage.is_native
        ? { label: 'native default', klass: 'bg-emerald-900/40 text-emerald-200' }
        : { label: 'legacy variant folder', klass: 'bg-amber-900/40 text-amber-200' }}
      <div class="text-foreground mb-2 text-xs">
        Mesh-swap permoflage.
        <span class="ml-1 rounded px-1.5 py-[1px] text-[10px] {variantTag.klass}">{variantTag.label}</span>
        The toolkit re-exports the hull GLB + skel_ext from the variant model_dir, and the extract lands in
        {#if permoflage.is_native}
          <strong>the same folder</strong> as the Vehicle's default extract (native).
        {:else}
          <strong>a new variant folder</strong> (separate from the Vehicle's default).
        {/if}
      </div>
      {#if !permoflage.is_native}
        <div class="border-amber-500/40 bg-amber-950/30 mb-2 rounded border px-3 py-2 text-xs text-amber-200">
          <strong>⚠ Legacy path — superseded by <code class="font-mono">exteriors[]</code>.</strong>
          The BASE ship's sidecar now carries this permoflage as a switchable
          <code class="font-mono">exteriors[]</code> entry (per-mount swaps + its own camo; the variant
          hull exports via <code class="font-mono">export_exterior_hulls</code> into
          <code class="font-mono">models/exteriors/</code>), selectable live in the ship viewer's
          <strong>Exteriors</strong> tab — no duplicate ship folder needed. Prefer extracting the
          <strong>Base ship</strong> row instead. This separate
          <code class="font-mono">__&lt;Variant&gt;</code> folder still works but duplicates
          armor/ballistics/skins (~30&nbsp;MB+) and is slated for removal at the unification cutover.
        </div>
      {/if}
      <table class="w-full text-xs">
        <tbody>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">exterior_id</th>
            <td class="py-1"><code class="font-mono">{permoflage.exterior_id}</code></td>
          </tr>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">display</th>
            <td class="py-1">{permoflage.display_name}</td>
          </tr>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">variant model_dir</th>
            <td class="py-1"><code class="font-mono">{permoflage.mesh_swap_dir || '?'}</code></td>
          </tr>
          <tr>
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">peculiarity</th>
            <td class="py-1">{permoflage.peculiarity || '—'}</td>
          </tr>
        </tbody>
      </table>
    {:else}
      <div class="text-foreground mb-2 text-xs">
        Non-mesh-swap (texture-only) permoflage.
        <span class="bg-sky-900/40 text-sky-200 ml-1 rounded px-1.5 py-[1px] text-[10px]">skin pack candidate</span>
        Geometry stays at <code class="font-mono">{vehicle.model_dir || '?'}</code>; running an extract would just
        rediscover this camo on top of the base sidecar. To layer it onto an already-extracted ship, scroll down
        to <strong>Add skin pack</strong> below — the "Fill from selected permoflage" button will pre-populate the
        form for you.
      </div>
      <table class="w-full text-xs">
        <tbody>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">exterior_id</th>
            <td class="py-1"><code class="font-mono">{permoflage.exterior_id}</code></td>
          </tr>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">display</th>
            <td class="py-1">{permoflage.display_name}</td>
          </tr>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">topology</th>
            <td class="py-1">
              <span class="bg-amber-900/40 text-amber-200 rounded px-1.5 py-[1px] text-[10px]">
                {TOPOLOGY_LABELS[permoflage.topology]}
              </span>
            </td>
          </tr>
          <tr class="border-border/40 border-b">
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">camouflage</th>
            <td class="py-1">
              {#if permoflage.camouflage}
                <code class="font-mono">{permoflage.camouflage}</code>
              {:else}
                —
              {/if}
            </td>
          </tr>
          <tr>
            <th class="text-muted-foreground py-1 pr-3 text-left font-medium">peculiarity</th>
            <td class="py-1">{permoflage.peculiarity || '—'}</td>
          </tr>
        </tbody>
      </table>
    {/if}
  </section>
</div>
