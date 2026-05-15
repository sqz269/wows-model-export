// Placement-matrix application + per-instance tagging for the ship viewer.
//
// `applyPlacementMatrix` is straight decompose into PRS.
//
// `applyAttachedMatrix` post-multiplies by `diag(-1, 1, 1, 1)` to mirror
// local-X. This is the gltFast parity fix: gltFast X-negates glTF vertex
// positions on import, three.js doesn't. Asymmetric attached meshes
// (e.g. AM6068_Cartridges_Hoshino, X 0 → +0.72) render inward-clipping in
// three.js without it; the gltFast pipeline already gets correct rendering
// for free. See migration/SHIP_TS_INVENTORY.md C.21 + memory
// `project_webview_xflip_asymmetric_meshes`.
//
// DO NOT re-add the conditional pre-Ry(180°) on `attached_y_flip`. That
// was an over-correction for an earlier schema; schema_v6
// attached_accessories.json bakes the convention-B basis conjugation into
// the matrix, so the consumer just decomposes verbatim.

import * as THREE from 'three';
import { PATCH_RE, lodLevelOfName } from './visibility';
import type { HullSectionKey, ShipPlacement, ShipSectionKey } from '$lib/types';
import type { ColorMaterials, PlacementColorEntry } from './color_mode';

const tmpMat4 = new THREE.Matrix4();
const localXFlipMat4 = new THREE.Matrix4().makeScale(-1, 1, 1);

export function applyPlacementMatrix(node: THREE.Object3D, matrix16: number[]): void {
  tmpMat4.fromArray(matrix16);
  tmpMat4.decompose(node.position, node.quaternion, node.scale);
}

export function applyAttachedMatrix(node: THREE.Object3D, matrix16: number[]): void {
  tmpMat4.fromArray(matrix16).multiply(localXFlipMat4);
  tmpMat4.decompose(node.position, node.quaternion, node.scale);
}

/** LOD policy. `'lod0'` shows only the default high-detail mesh (the
 *  user's normal viewing mode); `'all'` shows every level overlaid (a
 *  visually-noisy debug view); `'lodN'` (N ≥ 1) shows ONLY meshes at
 *  that level — useful for QAing the coarser-substitute geometry in
 *  isolation. Available levels per ship are reported by
 *  `ShipViewer.getAvailableLodLevels()`. */
export type LodPolicy = 'all' | `lod${number}`;

export interface TagOptions {
  section: ShipSectionKey;
  placement: ShipPlacement;
  colorMaterials: ColorMaterials;
  /** Pre-existing lod policy — drives whether non-LOD0 meshes are hidden. */
  lodPolicy: LodPolicy;
}

export interface TagResult {
  colorEntries: PlacementColorEntry[];
  /** All meshes under this placement bucketed by LOD level. The viewer
   *  merges these into the global per-level map for the LOD cascade. */
  meshesByLodLevel: Map<number, THREE.Object3D[]>;
}

/**
 * Tag every mesh under the placement instance with section + asset_id
 * for filtering, coloring, and dispose accounting. Builds the
 * `parent_mesh → instance` index needed for the damage-state cascade
 * (a single `setSeamState` hides every placement bound to a now-hidden
 * hull mesh).
 */
export function tagAndIndexInstance(
  root: THREE.Object3D,
  opts: TagOptions,
  placementsByMesh: Map<string, THREE.Object3D[]>,
): TagResult {
  const { section, placement: p, colorMaterials, lodPolicy } = opts;

  root.userData.section = section;
  root.userData.asset_id = p.asset_id;
  root.userData.instance_id = p.instance_id;
  root.userData.parent_section = p.parent_section ?? null;
  root.userData.parent_mesh = p.parent_mesh ?? null;

  if (p.parent_mesh) {
    let list = placementsByMesh.get(p.parent_mesh);
    if (!list) {
      list = [];
      placementsByMesh.set(p.parent_mesh, list);
    }
    list.push(root);
  }

  const hullSection: HullSectionKey | null = p.parent_section ?? null;
  // Patch-anchored placements use the desaturated section variant. The
  // semantic difference matters: a patch-anchored accessory hides when
  // EITHER its owning section OR the bridged-to adjacent section is
  // destroyed (the bridge can't span a missing endpoint), while an
  // intact-mesh-anchored accessory hides only when its owning section
  // dies. Visually flagging them makes damage experiments cleaner.
  const isPatchAnchored = !!p.parent_mesh && PATCH_RE.test(p.parent_mesh);
  const hullSectionMaterial = hullSection
    ? isPatchAnchored
      ? colorMaterials.hullSectionPatch[hullSection]
      : colorMaterials.hullSection[hullSection]
    : colorMaterials.hullSectionNull;

  const colorEntries: PlacementColorEntry[] = [];
  const meshesByLodLevel = new Map<number, THREE.Object3D[]>();

  root.traverse((obj) => {
    const m = obj as THREE.Mesh;
    if (!m.isMesh) return;
    const level = lodLevelOfName(m.name || '');
    let bucket = meshesByLodLevel.get(level);
    if (!bucket) {
      bucket = [];
      meshesByLodLevel.set(level, bucket);
    }
    bucket.push(m);
    if (level > 0 && lodPolicy === 'lod0') m.visible = false;

    colorEntries.push({
      mesh: m,
      originalMaterial: m.material,
      categoryMaterial: colorMaterials.category[section],
      hullSectionMaterial,
    });
  });

  return { colorEntries, meshesByLodLevel };
}
