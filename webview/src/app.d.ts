// Global ambient declarations for the Svelte app.

import type {} from 'svelte';

declare module '*.svelte' {
  import type { ComponentType } from 'svelte';
  const component: ComponentType;
  export default component;
}
