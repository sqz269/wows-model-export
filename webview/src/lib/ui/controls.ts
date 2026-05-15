// Shared Tailwind class slugs for the dense controls used across the
// Library + Ships pages. Pulling these into one module locks both
// pages to a single visual vocabulary — drift between the two was the
// motivation; previously each .svelte file redeclared the same chains.
//
// Usage:
//   import { rowCls, labelCls, inputBoxCls } from '$lib/ui/controls';
//
// Only the truly cross-page idioms live here. Page-local accents
// (e.g. ShipControls' `<details>` summary chevron) stay in the
// component that owns them — extracting one-off styles into a shared
// module obscures intent without removing duplication.

/** Checkbox / radio row: dense, baseline-aligned, label inline. */
export const rowCls = 'flex items-center gap-1.5 text-xs text-foreground';

/** Labelled-control column: small uppercase label stacked above the
 *  input. Used for `<select>` rows and any control wide enough to want
 *  its own caption. */
export const labelCls = 'flex flex-col gap-0.5 text-[11px] text-muted-foreground';

/** `<select>` / `<input>` chrome. Matches the popover/border palette so
 *  the dropdowns sit cleanly inside dark side panels. */
export const inputBoxCls =
  'h-7 rounded border border-border bg-popover px-1.5 text-xs ' +
  'text-foreground focus:outline-none focus:ring-2 ' +
  'focus:ring-ring/30 focus:border-ring';

/** Tab-button base for the bottom-panel tab strip. Combine with an
 *  active-vs-inactive class chain at the call site (active tabs use
 *  `border-primary text-foreground`; inactive use
 *  `border-transparent text-muted-foreground hover:text-foreground`). */
export const tabBtnBase =
  'px-3 py-1.5 text-[11px] uppercase tracking-wider font-semibold ' +
  'border-b-2 transition-colors focus:outline-none';
