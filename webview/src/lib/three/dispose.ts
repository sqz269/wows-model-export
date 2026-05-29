// Dispose a Three.js object tree, releasing GPU resources (geometry +
// materials, including any textures bound on materials).
//
// Why this isn't a one-liner: accessory clones in the ship viewer share
// geometry and material refs (`Object3D.clone` shallow-copies them), so a
// naive recursive dispose would `geometry.dispose()` once per CLONE
// rather than once per UNIQUE resource. Disposing the same WebGL handle
// twice is a no-op in three.js >=0.150, but disposing a still-referenced
// geometry breaks every other clone. WeakSet dedup is the correct shape.

import type * as THREE from 'three';

export function disposeTree(tree: THREE.Object3D): void {
  const seenGeom = new WeakSet<THREE.BufferGeometry>();
  const seenMat = new WeakSet<THREE.Material>();
  tree.traverse((obj) => {
    const m = obj as THREE.Mesh;
    if (!m.isMesh) return;

    // InstancedMesh owns an instanceMatrix (and optional instanceColor)
    // BufferAttribute that isn't released by geometry.dispose() — its
    // own `.dispose()` dispatches the event the WebGL renderer listens
    // for. The shared geometry/material are then handled by the normal
    // path below (via the WeakSet dedup, in case the same geometry is
    // also used as a non-instanced mesh elsewhere).
    const im = m as THREE.InstancedMesh;
    if (im.isInstancedMesh) {
      im.dispose();
    }

    const g = m.geometry;
    if (g && !seenGeom.has(g)) {
      g.dispose();
      seenGeom.add(g);
    }

    const mat = m.material;
    if (Array.isArray(mat)) {
      for (const x of mat) {
        if (!seenMat.has(x)) {
          disposeMaterial(x);
          seenMat.add(x);
        }
      }
    } else if (mat && !seenMat.has(mat)) {
      disposeMaterial(mat);
      seenMat.add(mat);
    }
  });
}

function disposeMaterial(mat: THREE.Material): void {
  // Dispose any texture maps bound on standard material slots. Skip the
  // env-map: it's shared across the scene and disposed by the scene
  // owner.
  const m = mat as THREE.MeshStandardMaterial;
  m.map?.dispose();
  m.normalMap?.dispose();
  m.metalnessMap?.dispose();
  m.roughnessMap?.dispose();
  m.aoMap?.dispose();
  m.emissiveMap?.dispose();
  mat.dispose();
}
