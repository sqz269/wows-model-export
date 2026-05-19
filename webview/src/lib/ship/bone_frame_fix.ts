// Runtime fix for accessory GLBs whose `.geometry` vertices and `.visual`
// bone tree were authored in 180°-rotated coordinate frames in BigWorld.
// The toolkit's `negate_z_transform` applies uniformly to both sides, so
// the asymmetry survives into the exported glTF — the muzzle bone lands
// at world z = −7 while the nearest mesh vertex is at z = +7 (verified on
// AGS145, USA BB 5"/54 secondary). Symptom: cradle/mantlet shroud tears
// away from the trunnion at non-zero pitch because the rotation pivot is
// in the wrong half-space relative to the geometry it influences.
//
// Why a webview fix and not toolkit-side: catching it during glTF export
// would need vertex positions in the Rust bone-tree emit path (currently
// only the bones are visible there). A post-process Python pass in
// `compose/turret_autorig.py` could mutate the GLB on disk, but that
// requires re-exporting every library asset and breaks any downstream
// consumer that hasn't been updated. Runtime-detected webview fix avoids
// the rebuild and keeps the on-disk format byte-identical.
//
// Trade-off: Unity + Blender exporters consume the same on-disk GLBs and
// will still see the broken bone tree. If they need the same fix, the
// algorithm here is the canonical reference for porting.
//
// Math:
//   bone tree is wrapped in a Y180 above Scene_Root (in WG conventions —
//   "Scene Root" with a space, but Three.js GLTFLoader stamps it as
//   "Scene_Root"). After updateMatrixWorld(true), every bone's matrixWorld
//   is left-multiplied by Y180. Then `Skeleton.calculateInverses()`
//   recomputes the inverse-bind matrices from the new bone world positions
//   — so at rest, `bone.matrixWorld × IBM = identity` still holds.
//
//   Under user rotation `Rx(θ)` on Rotate_X (which sits BELOW the Y180
//   wrap), the rotation is Y180-conjugated:
//       Y180 · Rx(θ) · Y180⁻¹ = Rx(−θ)
//   so a positive `+X` slider input now rotates the geometry the opposite
//   way relative to the world. The existing `computePitchSign` probe in
//   `turret_rig.ts` measures world-Y delta of a representative vertex
//   under a small `+Rx` test rotation and returns ±1; applying this fix
//   before the probe runs lets it pick up the conjugation and set
//   `pitchSign = −1` so user-facing "positive pitch = elevate" stays true.
//
// Scope: detection only triggers when the muzzle bone is > ~1m from any
// vertex AND a Y180 flip brings it within ~0.5m. AGM3019 (correctly-aligned
// main battery) sees Y180-dist 9.69m vs as-is 0.20m → ratio 48.5 → no fix
// applied. AGS145 (broken) sees Y180-dist 0.06m vs as-is 3.97m → ratio
// 0.016 → fix applied. Threshold leaves a safety margin for noisy assets.
//
// Bone choice for the probe: HP_gunFire1 is the right signal because it's
// a leaf far from the asset origin (large signal-to-noise). Rotate_Y and
// Rotate_X both sit near origin (and with X=0 by WG convention) — Y180 on
// (0, *, 0) is invariant, so they can't discriminate. Fallback to any
// HP_gunFire* node if HP_gunFire1 isn't named (rare; never seen in 130-
// asset corpus).

import * as THREE from 'three';

const SCENE_ROOT_NAMES = ['Scene_Root', 'Scene Root'];
const MUZZLE_PREFIX = 'HP_gunFire';

/** Diagnostic returned by `detectBoneFrameMismatch` / `maybeApplyBoneFrameFix`.
 *  Surfaced for logging / UI badges — none of the consumers branch on it
 *  yet, but having the numbers makes auditing the fix's decisions on new
 *  assets straightforward. */
export interface BoneFrameDiagnostic {
  /** True when the asset's bone tree was Y180-rotated to align with verts. */
  applied: boolean;
  /** Why we didn't detect/apply — set when `applied=false`. Empty string
   *  on success. Values: 'no_skinned_mesh', 'no_muzzle_bone', 'no_verts',
   *  'no_scene_root', 'aligned' (the normal case — bones match verts). */
  reason: string;
  /** Name of the bone used as the probe (e.g. 'HP_gunFire1'). */
  probeBoneName: string;
  /** Nearest vertex distance to the probe bone, as-is and after a
   *  hypothetical Y180 flip. Bone is mismatched when `y180 << asIs`. */
  distAsIs: number;
  distY180: number;
}

/** Threshold tuning. Triggers a Y180 fix when:
 *    - bone is > MIN_ABS_DIST_M away from any vert in current pose, AND
 *    - flipping by Y180 brings it < RATIO × current distance.
 *  Both gates must pass — single-condition versions misfire on tiny
 *  assets (low absolute distances on both sides) or symmetric assets
 *  (where Y180 happens to land near a vert but as-is is also close).
 *  Sweep on the corpus: AGM3019 ratio 48.5, AGS145 ratio 0.016 — a
 *  threshold of 0.5 leaves ~100× margin on both sides. */
const MIN_ABS_DIST_M = 1.0;
const Y180_RATIO_TRIGGER = 0.5;

/** Probe the bone-vs-vertex Z-half-space alignment without mutating
 *  anything. Used for logging or for a future "diagnose-only" mode.
 *
 *  Math note: positions are compared in ACCESSORY-LOCAL frame (the
 *  `root` parameter's local space), NOT world space. For an accessory
 *  placed at world `(X, Y, Z)` with some yaw, Y180-around-world-origin
 *  would land 2×(X, _, Z) away — not the local mirror we want. By
 *  pulling muzzle and verts into `root`'s frame first, Y180-around-
 *  origin becomes the correct "flip the accessory around its own
 *  yaw axis" reflection. The library view (root at world origin) and
 *  the ship view (root placed on the hull) then produce identical
 *  detection numbers. */
export function detectBoneFrameMismatch(
  root: THREE.Object3D,
): BoneFrameDiagnostic {
  const sm = findFirstSkinnedMesh(root);
  if (!sm) {
    return diag(false, 'no_skinned_mesh', '', 0, 0);
  }
  const muzzle = findMuzzleBone(root);
  if (!muzzle) {
    return diag(false, 'no_muzzle_bone', '', 0, 0);
  }
  // World matrices must be current before we read positions — callers
  // typically have already cloned but not necessarily updated.
  root.updateMatrixWorld(true);

  // Build `accessoryFromWorld = inv(root.matrixWorld)` so we can pull
  // any world-space point into accessory-local frame. For a clean
  // library-view load this collapses to identity (root.matrixWorld is
  // identity); for a ship-view placement it strips out the per-
  // instance hull placement.
  const accessoryFromWorld = root.matrixWorld.clone().invert();

  const muzzleLocal = new THREE.Vector3();
  muzzle.getWorldPosition(muzzleLocal).applyMatrix4(accessoryFromWorld);
  const muzzleLocalY180 = new THREE.Vector3(
    -muzzleLocal.x, muzzleLocal.y, -muzzleLocal.z,
  );

  const pos = sm.geometry.attributes.position;
  if (!pos || pos.count === 0) {
    return diag(false, 'no_verts', muzzle.name, 0, 0);
  }
  // Verts → mesh world → accessory-local. Precompose
  // `meshToAccessoryLocal = accessoryFromWorld × mesh.matrixWorld` so
  // we do one matrix per vert in the inner loop instead of two.
  const meshToAccessoryLocal = accessoryFromWorld.clone().multiply(sm.matrixWorld);
  const v = new THREE.Vector3();
  let bestAsIs = Infinity;
  let bestY180 = Infinity;
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i);
    v.applyMatrix4(meshToAccessoryLocal);
    const dAsIs = v.distanceTo(muzzleLocal);
    const dY180 = v.distanceTo(muzzleLocalY180);
    if (dAsIs < bestAsIs) bestAsIs = dAsIs;
    if (dY180 < bestY180) bestY180 = dY180;
  }

  const mismatched =
    bestAsIs > MIN_ABS_DIST_M && bestY180 < Y180_RATIO_TRIGGER * bestAsIs;

  return diag(
    /* applied */ mismatched,
    /* reason */ mismatched ? '' : 'aligned',
    muzzle.name,
    bestAsIs,
    bestY180,
  );
}

/** Detect + apply the Y180 fix if needed. Returns diagnostic — caller
 *  may forward it to telemetry or a debug panel. The mutation is
 *  contained to `root`'s subtree: we insert a Y180-rotated wrapper above
 *  Scene_Root (the WG convention top of the bone hierarchy), then
 *  recompute every Skeleton's inverse-bind matrices so rest pose stays
 *  visually identical.
 *
 *  Idempotent within a single clone, but not across clones — each call
 *  to `cloneAccessoryInstance` produces a fresh root, and this should
 *  run once per fresh root. */
export function maybeApplyBoneFrameFix(
  root: THREE.Object3D,
): BoneFrameDiagnostic {
  const d = detectBoneFrameMismatch(root);
  if (!d.applied) return d;

  // Find Scene_Root — the WG-canonical top of the bone tree.
  let sceneRoot: THREE.Object3D | null = null;
  root.traverse((o) => {
    if (sceneRoot) return;
    if (SCENE_ROOT_NAMES.includes(o.name)) sceneRoot = o;
  });
  if (!sceneRoot) {
    return { ...d, applied: false, reason: 'no_scene_root' };
  }
  const sceneRootNode: THREE.Object3D = sceneRoot;
  const parent = sceneRootNode.parent;
  if (!parent) {
    return { ...d, applied: false, reason: 'no_scene_root' };
  }

  // Insert a Y180 wrapper between `parent` and `Scene_Root`. The wrapper
  // gets a memorable name so it shows up in BoneInspector / debug
  // tooling as a clear indicator that this asset went through the fix.
  // We leave the mesh nodes (siblings of Scene_Root under `parent`)
  // untouched — Y180 only affects the bone subtree, which is what we
  // want: verts stay where they are, bones move to meet them.
  const wrapper = new THREE.Object3D();
  wrapper.name = 'BoneFrameFixY180';
  wrapper.quaternion.setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI);
  parent.remove(sceneRootNode);
  wrapper.add(sceneRootNode);
  parent.add(wrapper);

  // Force the new world matrices to propagate to every bone, then
  // RIGHT-MULTIPLY each inverse-bind matrix by Y180 so the skin renders
  // at the original mesh world position at rest pose.
  //
  // Math: with original GLB IBM = inverse(bone_chain_neutral), the
  // post-placement skinning sum at rest is
  //   sum = bone.matrixWorld × IBM_new
  //       = (placement × Y180 × bone_chain) × (IBM_orig × Y180)
  //       = placement × Y180 × bone_chain × inverse(bone_chain) × Y180
  //       = placement × Y180 × Y180
  //       = placement
  // which is what skinning produces WITHOUT the fix at rest — so the
  // mesh stays visually identical. Under user pitch `Rx(θ)` at Rotate_X,
  // the wrapper conjugates the rotation:
  //   sum = placement × Y180 × Rx(θ) × bone_chain × inverse(bone_chain) × Y180
  //       = placement × Y180 × Rx(θ) × Y180
  //       = placement × Rx(-θ)
  // so the world-frame rotation flips sign — `computePitchSign` catches
  // this and applies pitchSign=-1 so user-facing direction stays correct.
  //
  // Calling `skeleton.calculateInverses()` instead of this hand-mul would
  // recompute IBMs off post-placement bone.matrixWorld and double-count
  // the placement (mesh renders far from where it should — observed
  // empirically on AGS145+AGM034 on Montana).
  parent.updateMatrixWorld(true);
  const y180Mat = new THREE.Matrix4().makeRotationY(Math.PI);
  // CRITICAL: SkeletonUtils.clone produces N Skeleton INSTANCES for N
  // SkinnedMeshes (one per LOD), but the underlying `boneInverses`
  // ARRAY is shared by reference across all of them — AND across every
  // other clone of the same template (Skeleton's clone() does
  // `new Skeleton(bones, boneInverses)` with direct assignment).
  // Mutating that array in place would Y180 it once per LOD per
  // placement, cascading to other clones and silently no-op-ing on
  // even counts (10 AGS145 placements × N LODs = no net change).
  //
  // Fix: REPLACE each skeleton's `boneInverses` with a freshly-cloned
  // array of Y180-multiplied matrices. This detaches the cloned
  // skeleton from the shared array; mutations stay private to this
  // instance.
  const replacedFor = new Set<THREE.Matrix4[]>();
  const newIBMsByOrig = new Map<THREE.Matrix4[], THREE.Matrix4[]>();
  root.traverse((o) => {
    const sm = o as THREE.SkinnedMesh;
    if (!sm.isSkinnedMesh || !sm.skeleton) return;
    const origIBMs = sm.skeleton.boneInverses;
    let newIBMs = newIBMsByOrig.get(origIBMs);
    if (!newIBMs) {
      // Build a per-clone copy with Y180 right-multiplied in.
      newIBMs = origIBMs.map((ibm) => ibm.clone().multiply(y180Mat));
      newIBMsByOrig.set(origIBMs, newIBMs);
    }
    sm.skeleton.boneInverses = newIBMs;
    replacedFor.add(origIBMs);
    sm.skeleton.update();
  });
  void replacedFor;

  return d;
}

function findFirstSkinnedMesh(root: THREE.Object3D): THREE.SkinnedMesh | null {
  let found: THREE.SkinnedMesh | null = null;
  root.traverse((o) => {
    if (found) return;
    const sm = o as THREE.SkinnedMesh;
    if (sm.isSkinnedMesh) found = sm;
  });
  return found;
}

function findMuzzleBone(root: THREE.Object3D): THREE.Object3D | null {
  // Prefer HP_gunFire1 — the canonical primary muzzle. Falls back to any
  // HP_gunFire* node if the canonical one isn't present (defensive; not
  // observed in the live 130-asset corpus). HP_gunFireEffect is filtered
  // out because it's a shared flash-effect anchor, not a barrel tip, and
  // can sit at a different position from the actual muzzles.
  let primary: THREE.Object3D | null = null;
  let fallback: THREE.Object3D | null = null;
  root.traverse((o) => {
    if (primary) return;
    if (!o.name) return;
    if (o.name === `${MUZZLE_PREFIX}1`) primary = o;
    else if (
      !fallback &&
      o.name.startsWith(MUZZLE_PREFIX) &&
      o.name !== `${MUZZLE_PREFIX}Effect`
    ) {
      fallback = o;
    }
  });
  return primary ?? fallback;
}

function diag(
  applied: boolean,
  reason: string,
  probeBoneName: string,
  distAsIs: number,
  distY180: number,
): BoneFrameDiagnostic {
  return { applied, reason, probeBoneName, distAsIs, distY180 };
}
