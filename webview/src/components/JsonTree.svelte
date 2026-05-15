<script lang="ts">
  // Recursive collapsible JSON viewer. Renders arbitrary parsed JSON
  // (object/array/string/number/boolean/null) with one-click expand on
  // objects and arrays.
  //
  // The component is intentionally minimal — no path-based selection,
  // no syntax highlighting beyond muted-vs-foreground colour, no search
  // (yet). The GameParams browser route is a prototype; if this grows
  // legs we can add jq-style filtering, copy-paths, etc.
  //
  // Default expansion: only the root level. Click `▶` / `▼` to toggle
  // a node. Empty containers (`{}` / `[]`) render inline.

  import { untrack } from 'svelte';
  import JsonTreeSelf from './JsonTree.svelte';

  interface Props {
    /** The value to render. Any JSON-shaped thing. */
    value: unknown;
    /** Property name / array index for this node (omitted at root). */
    label?: string | number | null;
    /** Depth in the tree — drives the default-collapsed cutoff. */
    depth?: number;
    /** Force-open at start, regardless of depth. The top-level
     *  container is rendered with `defaultOpen` so the user sees
     *  *something* without clicking. */
    defaultOpen?: boolean;
  }

  const { value, label = null, depth = 0, defaultOpen = false }: Props = $props();

  function valueKind(v: unknown): 'null' | 'bool' | 'number' | 'string' | 'array' | 'object' {
    if (v === null) return 'null';
    if (Array.isArray(v)) return 'array';
    const t = typeof v;
    if (t === 'boolean') return 'bool';
    if (t === 'number') return 'number';
    if (t === 'string') return 'string';
    return 'object';
  }

  const kind = $derived(valueKind(value));
  const isContainer = $derived(kind === 'array' || kind === 'object');
  const entries = $derived.by(() => {
    if (kind === 'array') {
      const arr = value as unknown[];
      return arr.map((v, i) => ({ key: i, value: v }));
    }
    if (kind === 'object') {
      return Object.entries(value as Record<string, unknown>).map(([k, v]) => ({
        key: k,
        value: v,
      }));
    }
    return [];
  });

  // Default-open: only the root (depth 0) or callers that opt in.
  // Below that, containers start collapsed to keep the initial render
  // fast even for very deep records (e.g. a Ship entity has dozens of
  // module slots). `defaultOpen` + `depth` are init-only — we don't
  // expect them to change after mount, so untrack to silence the
  // svelte-check `state_referenced_locally` warning.
  let open = $state(untrack(() => defaultOpen || depth === 0));

  function summary(): string {
    if (kind === 'array') {
      const n = (value as unknown[]).length;
      return `Array(${n})`;
    }
    if (kind === 'object') {
      const n = Object.keys(value as object).length;
      return `Object(${n})`;
    }
    return '';
  }

  function formatPrimitive(v: unknown): string {
    if (v === null) return 'null';
    if (typeof v === 'string') return JSON.stringify(v);
    return String(v);
  }
</script>

<div class="font-mono text-[11px] leading-relaxed">
  {#if isContainer}
    <button
      type="button"
      class="hover:text-foreground inline-flex items-center gap-1 bg-transparent p-0 text-left text-muted-foreground"
      onclick={() => (open = !open)}
    >
      <span class="inline-block w-3 select-none">{open ? '▼' : '▶'}</span>
      {#if label !== null}
        <span class="text-foreground">{label}</span>
        <span class="opacity-60">:</span>
      {/if}
      {#if entries.length === 0}
        <span>{kind === 'array' ? '[]' : '{}'}</span>
      {:else}
        <span class="opacity-70">{summary()}</span>
      {/if}
    </button>
    {#if open && entries.length > 0}
      <div class="ml-3 border-l border-border/40 pl-3">
        {#each entries as e (e.key)}
          <JsonTreeSelf value={e.value} label={e.key} depth={depth + 1} />
        {/each}
      </div>
    {/if}
  {:else}
    <div class="inline-flex items-baseline gap-1">
      <span class="inline-block w-3 select-none"></span>
      {#if label !== null}
        <span class="text-foreground">{label}</span>
        <span class="text-muted-foreground opacity-60">:</span>
      {/if}
      <span
        class:text-emerald-300={kind === 'string'}
        class:text-amber-300={kind === 'number'}
        class:text-sky-300={kind === 'bool'}
        class:text-muted-foreground={kind === 'null'}
      >
        {formatPrimitive(value)}
      </span>
    </div>
  {/if}
</div>
