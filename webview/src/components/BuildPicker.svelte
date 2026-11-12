<script lang="ts">
  // Topnav game-build picker. Lists every build the backend can read (the
  // install's and the build archive's) and switches which one this browser
  // views: the choice is the `wows_build` cookie, and the backend answers
  // every /api and /repo request from that build's workspace
  // (`<workspace>/builds/<build>/`, reading that build's game data). The
  // page reloads on a switch so every tab refetches. Viewing a build other
  // than the current one shows a badge, and an extract run from any page
  // lands in that build's workspace.
  import { onMount } from 'svelte';
  import { fetchBuildSources, setViewedBuild, type BuildSources } from '$lib/api';

  let sources = $state<BuildSources | null>(null);
  let error = $state<string | null>(null);

  const selected = $derived(sources?.selected ?? null);
  const viewingOther = $derived(
    sources !== null && selected !== null && selected !== sources.current_build,
  );
  /** The cookie names a build the backend can't serve (shown as current). */
  const unserved = $derived(
    sources !== null && sources.requested !== null && sources.requested !== selected,
  );

  onMount(() => {
    fetchBuildSources()
      .then((s) => (sources = s))
      .catch((err) => (error = err instanceof Error ? err.message : String(err)));
  });

  function label(b: BuildSources['builds'][number]): string {
    const where = b.current ? 'current' : (b.reads ?? 'unreadable');
    const n = b.exports.ships + b.exports.maps;
    return `${b.build} · ${where}${n ? ` · ${b.exports.ships} ships, ${b.exports.maps} maps` : ''}`;
  }

  function onChange(e: Event) {
    const value = Number((e.currentTarget as HTMLSelectElement).value);
    if (!sources || value === selected) return;
    setViewedBuild(value === sources.current_build ? null : value);
    location.reload();
  }
</script>

{#if sources && sources.builds.length > 0}
  <label class="flex items-center gap-1.5 text-[11px] text-muted-foreground">
    <span>Game build</span>
    <select
      class="h-6 rounded border px-1 font-mono text-[11px] text-foreground focus:outline-none {viewingOther
        ? 'border-amber-500/60 bg-amber-500/10'
        : 'border-border bg-popover'}"
      value={selected}
      onchange={onChange}
      aria-label="Game build to view and export"
      data-testid="build-picker"
      title={viewingOther
        ? `Viewing archived build ${selected}: pages show, and extracts write to, ${sources.builds.find((b) => b.build === selected)?.workspace}`
        : 'The build the install runs now. Pick an archived build to view and export from it.'}
    >
      {#each sources.builds as b (b.build)}
        <option value={b.build} disabled={b.reads === null}>{label(b)}</option>
      {/each}
    </select>
    {#if viewingOther}
      <span
        class="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-400"
        data-testid="build-badge">archived build</span
      >
    {:else if unserved}
      <span
        class="rounded bg-destructive/15 px-1.5 py-0.5 text-[10px] text-destructive"
        title="The build this browser asked for can't be read any more; showing the current build."
        >build {sources.requested} unavailable</span
      >
    {/if}
  </label>
{:else if error}
  <span class="text-[11px] text-muted-foreground" title={error}>build: ?</span>
{/if}
