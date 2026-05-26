// Hitbox / damage-module overlay. The hull GLB's `Hitboxes` group carries
// the `CM_SB_*` splash-box cubes; each box name keys into the sidecar's
// `hitbox.boxes[name]` for its damage section + `hl_type`. We tint each box
// translucent by hit-location category and add crisp edge lines so the
// boxy volumes read against the hull.
//
// Lazy-prepared on first enable and disposed with the ship. Edge LineSegments
// are parented under each box mesh (so they inherit the box transform and ride
// the group's visibility); their geometry is released by the hull's
// disposeTree, the shared materials by disposeHitboxView.

import * as THREE from 'three';
import type { SidecarHitboxBox } from '$lib/types';
import { shortMeshName } from './visibility';

/** Ordered legend: hit-location category → colour + label. Matched against
 *  the box's `hl_type` (and `section` as a fallback) by keyword so the
 *  GameParams typo `supersctructure_hitlocation` and section codes both
 *  resolve. First keyword hit wins, so order matters. */
export const HITBOX_LEGEND: ReadonlyArray<{ key: string; label: string; hex: string }> = [
  { key: 'citadel', label: 'Citadel', hex: '#e5484d' },
  { key: 'magazine', label: 'Magazine', hex: '#d6409f' },
  { key: 'engine', label: 'Engine', hex: '#f2810d' },
  { key: 'steering', label: 'Steering', hex: '#f5c518' },
  { key: 'artillery', label: 'Barbette', hex: '#8e4ec6' },
  { key: 'atba', label: 'Secondary', hex: '#7c5cff' },
  { key: 'airdef', label: 'AA', hex: '#2e9fff' },
  { key: 'torp', label: 'Torpedo', hex: '#12b5cb' },
  { key: 'super', label: 'Superstructure', hex: '#8b95a5' },
  { key: 'simple', label: 'Hull', hex: '#5b6573' },
];

const DEFAULT_STYLE = { label: 'Other', hex: '#6b7280' };

/** Resolve a box's legend style from its `hl_type` / `section`. */
export function hitboxStyleFor(box: SidecarHitboxBox | undefined): { label: string; hex: string } {
  const hay = `${box?.hl_type ?? ''} ${box?.section ?? ''}`.toLowerCase();
  for (const e of HITBOX_LEGEND) {
    if (hay.includes(e.key)) return { label: e.label, hex: e.hex };
  }
  return DEFAULT_STYLE;
}

export interface HitboxMeshEntry {
  mesh: THREE.Mesh;
  originalMaterial: THREE.Material | THREE.Material[];
  /** Translucent fill the box swaps onto. Shared per colour. */
  fill: THREE.Material;
  /** Edge overlay, parented under the box mesh; toggled with the view. */
  edges: THREE.LineSegments;
}

/**
 * Walk the `Hitboxes` group, tint each `CM_SB_*` cube by its damage category,
 * and attach an edge overlay. Returns reversible swap entries.
 */
export function prepareHitboxMeshes(
  hitboxGroup: THREE.Object3D,
  boxes: Record<string, SidecarHitboxBox>,
): HitboxMeshEntry[] {
  const fillCache = new Map<string, THREE.MeshBasicMaterial>();
  const edgeCache = new Map<string, THREE.LineBasicMaterial>();
  const entries: HitboxMeshEntry[] = [];

  const fillFor = (hex: string): THREE.MeshBasicMaterial => {
    let m = fillCache.get(hex);
    if (!m) {
      m = new THREE.MeshBasicMaterial({
        color: new THREE.Color(hex),
        transparent: true,
        opacity: 0.22,
        depthWrite: false,
        side: THREE.DoubleSide,
      });
      fillCache.set(hex, m);
    }
    return m;
  };
  const edgeFor = (hex: string): THREE.LineBasicMaterial => {
    let m = edgeCache.get(hex);
    if (!m) {
      m = new THREE.LineBasicMaterial({
        color: new THREE.Color(hex),
        transparent: true,
        opacity: 0.9,
      });
      edgeCache.set(hex, m);
    }
    return m;
  };

  hitboxGroup.traverse((obj) => {
    const mesh = obj as THREE.Mesh;
    if (!mesh.isMesh) return;
    const geom = mesh.geometry as THREE.BufferGeometry;
    if (!geom.getAttribute('position')) return;

    const name = shortMeshName(mesh.name || '');
    const style = hitboxStyleFor(boxes[name] ?? boxes[mesh.name]);

    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom), edgeFor(style.hex));
    edges.name = `${name}__edges`;
    edges.visible = false;
    edges.renderOrder = 3;
    mesh.add(edges);

    entries.push({
      mesh,
      originalMaterial: mesh.material,
      fill: fillFor(style.hex),
      edges,
    });
  });

  return entries;
}

export function applyHitboxView(entries: HitboxMeshEntry[], on: boolean): void {
  for (const e of entries) {
    e.mesh.material = on ? e.fill : e.originalMaterial;
    e.edges.visible = on;
  }
}

export function disposeHitboxView(entries: HitboxMeshEntry[]): void {
  // Dedupe shared fill/edge materials (cached per colour during prep). The
  // EdgesGeometry instances are unique per box and released by the hull's
  // disposeTree along with the box meshes.
  const seen = new Set<THREE.Material>();
  for (const e of entries) {
    for (const m of [e.fill, e.edges.material as THREE.Material]) {
      if (!seen.has(m)) {
        seen.add(m);
        m.dispose();
      }
    }
  }
}
