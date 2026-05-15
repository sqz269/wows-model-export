// Walk a freshly-loaded hull GLB scene, bucket meshes by name +
// damage-variant + LOD, and build the by-name index that drives
// per-section visibility cascades.
//
// The hull GLB has top-level groups Hull / Armor / Hitboxes
// (per docs/architecture.md). Mesh names within follow the
// `_crack_` / `_patch_` / `_lodN` convention used by `resolveMeshVisibility`.

import type * as THREE from 'three';
import {
  CRACK_RE,
  HULL_HIDDEN_GROUPS,
  PATCH_RE,
  lodLevelOfName,
  shortMeshName,
} from './visibility';

export interface HullGroupRef {
  name: string;
  node: THREE.Object3D;
}

export interface ClassifiedHull {
  /** Top-level groups (Hull / Armor / Hitboxes / …) for the controls panel. */
  groups: HullGroupRef[];
  /** Short-name → renderer list (multiple primitives may share a name). */
  renderersByMesh: Map<string, THREE.Mesh[]>;
  /** Meshes whose name matches `_crack_` or `_patch_` — damage variants. */
  damageMeshes: THREE.Object3D[];
  /** All meshes bucketed by LOD level. Level 0 is the default high-
   *  detail mesh (no `_lodN` suffix); levels 1..N are progressively
   *  coarser substitutes. `lodPolicy === 'lod0'` hides every level > 0;
   *  `lodPolicy === 'lodN'` shows only level N. */
  meshesByLodLevel: Map<number, THREE.Object3D[]>;
}

export interface ClassifyOptions {
  /** Hide non-LOD0 meshes from the start (`lod0` policy). Default: true. */
  hideLowLod?: boolean;
  /** Hide damage-variant meshes (cracks / patches) from the start. Default: true. */
  hideDamageVariants?: boolean;
}

export function classifyHullScene(
  root: THREE.Object3D,
  opts: ClassifyOptions = {},
): ClassifiedHull {
  const { hideLowLod = true, hideDamageVariants = true } = opts;

  const out: ClassifiedHull = {
    groups: [],
    renderersByMesh: new Map(),
    damageMeshes: [],
    meshesByLodLevel: new Map(),
  };

  for (const child of root.children) {
    if (!child.name) continue;
    out.groups.push({ name: child.name, node: child });
    if (HULL_HIDDEN_GROUPS.has(child.name)) child.visible = false;
  }

  root.traverse((obj) => {
    const m = obj as THREE.Mesh;
    if (!m.isMesh) return;
    const name = m.name || '';
    const short = shortMeshName(name);
    let list = out.renderersByMesh.get(short);
    if (!list) {
      list = [];
      out.renderersByMesh.set(short, list);
    }
    list.push(m);

    const level = lodLevelOfName(name);
    let bucket = out.meshesByLodLevel.get(level);
    if (!bucket) {
      bucket = [];
      out.meshesByLodLevel.set(level, bucket);
    }
    bucket.push(m);
    if (level > 0 && hideLowLod) m.visible = false;

    if (CRACK_RE.test(name) || PATCH_RE.test(name)) {
      out.damageMeshes.push(m);
      if (hideDamageVariants) m.visible = false;
    }
  });

  return out;
}
