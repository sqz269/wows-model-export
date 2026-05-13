<script lang="ts">
  // Ship-picker sidebar. Renders the ship list with per-section badges +
  // emits a `select` event when the user clicks a row.

  import type { ShipSummary } from '$lib/types';

  interface Props {
    ships: ShipSummary[];
    activeName: string | null;
    onSelect: (ship: ShipSummary) => void;
  }

  const { ships, activeName, onSelect }: Props = $props();

  const TIERS = (t: number | null) => (t === null ? '' : `T${t}`);
</script>

<aside class="sidebar">
  <header>
    <h1>Ships</h1>
    <div class="counter">
      {ships.length} ship{ships.length === 1 ? '' : 's'} indexed
    </div>
  </header>
  <ul class="ship-list">
    {#each ships as ship (ship.name)}
      <li>
        <button
          type="button"
          class="row"
          class:active={activeName === ship.name}
          onclick={() => onSelect(ship)}
        >
          <div class="row-top">
            <span class="name">{ship.display_name}</span>
            {#if ship.ship_class}
              <span class="chip">{ship.ship_class}</span>
            {/if}
            {#if ship.tier}
              <span class="chip">{TIERS(ship.tier)}</span>
            {/if}
          </div>
          <div class="row-bot">
            <span>{ship.section_counts.turrets}T</span>
            <span>{ship.section_counts.secondaries}S</span>
            <span>{ship.section_counts.antiair}AA</span>
            <span>{ship.section_counts.torpedoes}TT</span>
            <span>{ship.section_counts.accessories}·</span>
            {#if ship.nation}
              <span class="nation">{ship.nation}</span>
            {/if}
          </div>
        </button>
      </li>
    {/each}
    {#if ships.length === 0}
      <li class="empty">
        No ships in workspace. Run <code>wows-ingest-ship &lt;Ship&gt;</code> to add one.
      </li>
    {/if}
  </ul>
</aside>

<style>
  .sidebar {
    flex: 0 0 280px;
    background: var(--bg-side);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  header {
    padding: 12px 14px 8px;
    border-bottom: 1px solid var(--border);
  }
  h1 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }
  .counter {
    margin-top: 4px;
    font-size: 11px;
    color: var(--fg-muted);
  }
  .ship-list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    flex: 1 1 auto;
  }
  .row {
    display: block;
    width: 100%;
    padding: 7px 14px;
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border);
    border-left: 3px solid transparent;
    text-align: left;
    color: var(--fg);
    cursor: pointer;
  }
  .row:hover {
    background: var(--bg-elev);
  }
  .row.active {
    background: var(--accent-bg);
    border-left-color: var(--accent);
  }
  .row-top {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .name {
    font-weight: 500;
  }
  .row-bot {
    margin-top: 2px;
    display: flex;
    gap: 8px;
    font-size: 11px;
    color: var(--fg-muted);
    font-variant-numeric: tabular-nums;
  }
  .nation {
    margin-left: auto;
    text-transform: lowercase;
  }
  .chip {
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 3px;
    background: var(--bg-elev-2);
    color: var(--fg-dim);
    letter-spacing: 0.04em;
  }
  .empty {
    padding: 14px;
    color: var(--fg-muted);
    font-size: 12px;
  }
</style>
