<script lang="ts">
  // Generic form for one consumer action. Renders one input per declared
  // param; widget choice is keyed off `param.kind`. Falls back to a
  // text input for unknown kinds so a future server-side kind doesn't
  // break the page.

  import type { ConsumerAction } from '$lib/types/consumers';
  import type { ExtractedShip } from '$lib/types/extract';

  interface Props {
    action: ConsumerAction;
    /** Source list for `ships_picker` widgets. Empty list disables the
     *  picker with a hint so the user knows to ingest a ship first. */
    extractedShips: ExtractedShip[];
    disabled: boolean;
    onSubmit: (body: Record<string, unknown>) => void;
  }

  const { action, extractedShips, disabled, onSubmit }: Props = $props();

  function defaultFor(kind: string, fallback: unknown): unknown {
    if (fallback !== undefined && fallback !== null) return fallback;
    if (kind === 'bool') return false;
    if (kind === 'ships_picker') return [] as string[];
    return '';
  }

  // Local state keyed by param.id. Initialised on first $effect tick
  // (Svelte's $state initialiser only captures the prop's initial value;
  // re-init lives in the effect so action prop swaps reset cleanly).
  let values = $state<Record<string, unknown>>({});
  let lastActionKey = $state<string | null>(null);
  $effect(() => {
    const key = `${action.id}:${action.params.map((p) => p.id).join(',')}`;
    if (key !== lastActionKey) {
      lastActionKey = key;
      values = Object.fromEntries(action.params.map((p) => [p.id, defaultFor(p.kind, p.default)]));
    }
  });

  function setValue(id: string, v: unknown): void {
    values = { ...values, [id]: v };
  }

  function toggleShip(id: string, name: string, checked: boolean): void {
    const current = (values[id] as string[]) ?? [];
    const next = checked ? Array.from(new Set([...current, name])) : current.filter((s) => s !== name);
    setValue(id, next);
  }

  function submit(): void {
    onSubmit({ ...values });
  }
</script>

<div class="border-border rounded border px-3 py-3">
  <div class="mb-2 flex items-center justify-between gap-3">
    <div>
      <h4 class="text-foreground text-xs font-semibold uppercase tracking-wide">{action.label}</h4>
      {#if action.description}
        <p class="text-muted-foreground mt-0.5 text-[11px]">{action.description}</p>
      {/if}
    </div>
    <button
      type="button"
      {disabled}
      onclick={submit}
      class="bg-primary text-primary-foreground hover:bg-primary/90 rounded px-3 py-1.5 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-60"
    >
      Run
    </button>
  </div>

  <div class="flex flex-col gap-3">
    {#each action.params as p (p.id)}
      <div class="flex flex-col gap-1 text-xs">
        <div class="text-muted-foreground">{p.label}</div>

        {#if p.kind === 'bool'}
          <label class="text-foreground inline-flex items-center gap-2">
            <input
              type="checkbox"
              checked={values[p.id] as boolean}
              {disabled}
              onchange={(e) => setValue(p.id, (e.currentTarget as HTMLInputElement).checked)}
              class="accent-primary"
            />
            <span class="text-[11px]">{p.description || 'enable'}</span>
          </label>
        {:else if p.kind === 'ships_picker'}
          {@const picked = (values[p.id] as string[]) ?? []}
          <div class="border-border bg-popover/40 max-h-40 overflow-auto rounded border p-1.5">
            {#if extractedShips.length === 0}
              <p class="text-muted-foreground p-1.5 text-[11px]">
                No extracted ships found. Run an extract first, or leave the picker empty
                if this action accepts no ships.
              </p>
            {:else}
              {#each extractedShips as s (s.name)}
                <label class="text-foreground hover:bg-popover flex items-center gap-2 rounded px-1.5 py-0.5 text-[11px]">
                  <input
                    type="checkbox"
                    checked={picked.includes(s.name)}
                    {disabled}
                    onchange={(e) => toggleShip(p.id, s.name, (e.currentTarget as HTMLInputElement).checked)}
                    class="accent-primary"
                  />
                  <span class="font-mono">{s.name}</span>
                </label>
              {/each}
            {/if}
          </div>
          {#if p.description}
            <small class="text-muted-foreground/70 text-[10px]">{p.description}</small>
          {/if}
        {:else}
          <input
            type="text"
            value={(values[p.id] as string) ?? ''}
            {disabled}
            oninput={(e) => setValue(p.id, (e.currentTarget as HTMLInputElement).value)}
            placeholder={p.description}
            class="border-border bg-popover/40 text-foreground rounded border px-2 py-1 font-mono text-[11px]"
          />
        {/if}
      </div>
    {/each}
  </div>
</div>
