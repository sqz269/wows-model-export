// ResizeObserver wiring for a Three.js viewer hosted in a flex container.
//
// Watches `container.clientWidth/Height` and re-syncs the renderer + camera
// + (optional) composer. Returns a disposer.

import type * as THREE from 'three';

export interface ResizeOptions {
  container: HTMLElement;
  renderer: THREE.WebGLRenderer;
  camera: THREE.PerspectiveCamera;
  /** Optional post-FX composer to keep in sync. */
  onResize?: (w: number, h: number) => void;
}

export function observeResize(opts: ResizeOptions): () => void {
  const { container, renderer, camera, onResize } = opts;

  const apply = () => {
    const w = container.clientWidth || 1;
    const h = container.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    onResize?.(w, h);
  };

  const ro = new ResizeObserver(apply);
  ro.observe(container);
  apply();

  return () => ro.disconnect();
}
