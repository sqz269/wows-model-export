// Standard helpers shadcn-svelte components import.
//
// `cn` merges class names with Tailwind-aware deduplication so later
// arguments win over earlier ones for conflicting utilities (e.g.
// `cn('p-2', 'p-4')` → `'p-4'`). `clsx` handles conditional class
// chunks; `tailwind-merge` resolves Tailwind conflicts. Together they
// are the de facto idiom across the React + Svelte shadcn ecosystem
// and every generated component will `import { cn } from '$lib/utils'`.
//
// `WithoutChild` / `WithoutChildren` / `WithoutChildrenOrChild` are the
// prop-trimming helpers shadcn-svelte wrapper components use when they
// want to expose the underlying bits-ui primitive's prop surface but
// take over rendering the slot themselves (so consumers pass classes /
// data / refs, not children).

import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export type WithoutChild<T> = T extends { child?: unknown } ? Omit<T, 'child'> : T;
export type WithoutChildren<T> = T extends { children?: unknown } ? Omit<T, 'children'> : T;
export type WithoutChildrenOrChild<T> = WithoutChildren<WithoutChild<T>>;

/** Re-exported convenience type for component prop intersections. */
export type WithElementRef<T, U extends HTMLElement = HTMLElement> = T & {
  ref?: U | null;
};
