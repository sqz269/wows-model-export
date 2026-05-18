// Turret rig driver. Reads the WG bone tree baked into accessory GLBs by
// `gltf_export` (`Rotate_Y` for yaw, `Rotate_X` / `Rotate_X1` for pitch)
// and exposes per-placement setters the UI can drive.
//
// The skin data itself comes from the WG `.geometry` file (`iiiww` vertex
// format → glTF JOINTS_0/WEIGHTS_0). Three.js handles the actual
// deformation through its SkinnedMesh class; we just rotate the bones and
// let the skeleton update propagate.
//
// Per-instance cloning uses `SkeletonUtils.clone` instead of
// `Object3D.clone(true)`: the default clone shares the underlying
// `Skeleton`, so rotating one turret's `Rotate_Y` would rotate every
// turret bound to the same template. `SkeletonUtils.clone` deep-copies
// the bones and rewires each SkinnedMesh to its private skeleton.

import * as THREE from 'three';
import { clone as skeletonClone } from 'three/addons/utils/SkeletonUtils.js';

/** Per-placement rig handle. Empty for accessories without skin data
 *  (AA mounts, static decoratives) — managers skip those silently. */
export interface TurretRig {
  /** Top-level cloned accessory root carrying `userData.instance_id`. */
  root: THREE.Object3D;
  /** Sidecar instance id (stable across reloads). */
  instanceId: string;
  /** Library asset id (drives debug labels, future per-asset clamps). */
  assetId: string;
  /** Yaw node — typically named `Rotate_Y`. Always rotated around its
   *  local +Y axis. `null` when the visual lacks a yaw bone (rare;
   *  fixed-bearing AA mounts). Three.js's GLTFLoader marks nodes as
   *  `Bone` only if they appear in a Skin.joints palette — the parents
   *  of joints (where the WG yaw/pitch sit) stay plain Object3D, so we
   *  type this as Object3D and walk by name. */
  yaw: THREE.Object3D | null;
  /** Pitch nodes — usually one (`Rotate_X`) for triple/twin turrets,
   *  two (`Rotate_X` + `Rotate_X1`) for quad turrets with independent
   *  barrel-pair elevation. All driven together; per-pair control would
   *  be a future split. */
  pitch: THREE.Object3D[];
  /** Initial local quaternions captured at extraction time so we can
   *  apply rotations relative to bind pose. */
  yawRest: THREE.Quaternion | null;
  pitchRest: THREE.Quaternion[];
}

const YAW_BONE_NAME = 'Rotate_Y';
const PITCH_BONE_NAMES = ['Rotate_X', 'Rotate_X1'];

/** Deep-clone an accessory template so each placement gets its own
 *  bones. Falls back to a plain `clone(true)` for templates without a
 *  SkinnedMesh — `SkeletonUtils.clone` is safe to use there too, but
 *  skipping it avoids the small overhead on the 80%+ of accessories
 *  that ship static. */
export function cloneAccessoryInstance(template: THREE.Object3D): THREE.Object3D {
  let hasSkin = false;
  template.traverse((o) => {
    if ((o as THREE.SkinnedMesh).isSkinnedMesh) hasSkin = true;
  });
  if (!hasSkin) return template.clone(true);
  return skeletonClone(template);
}

/** Walk a cloned accessory instance for the canonical WG rig nodes.
 *  Returns `null` when no yaw/pitch nodes are present (e.g. AA mounts
 *  whose visuals are flat). The match is by NAME because Three.js's
 *  GLTFLoader only marks `Skin.joints` palette members as `Bone`; the
 *  WG `Rotate_Y` / `Rotate_X` sit above the joints in the hierarchy and
 *  stay plain Object3D. Rotating a parent Object3D still propagates to
 *  the bone-classified descendants because Three.js walks the parent
 *  chain when computing `matrixWorld` each frame, so skinning updates
 *  correctly. */
export function extractTurretRig(
  root: THREE.Object3D,
  assetId: string,
  instanceId: string,
): TurretRig | null {
  let yaw: THREE.Object3D | null = null;
  const pitch: THREE.Object3D[] = [];
  root.traverse((o) => {
    if (!o.name) return;
    if (o.name === YAW_BONE_NAME && !yaw) {
      yaw = o;
    } else if (PITCH_BONE_NAMES.includes(o.name)) {
      pitch.push(o);
    }
  });
  if (!yaw && pitch.length === 0) return null;
  return {
    root,
    instanceId,
    assetId,
    yaw,
    pitch,
    yawRest: yaw ? (yaw as THREE.Object3D).quaternion.clone() : null,
    pitchRest: pitch.map((b) => b.quaternion.clone()),
  };
}

/** Apply a yaw + pitch delta (radians, relative to bind pose) to a rig.
 *  Yaw rotates around the bone's local +Y axis; pitch around local +X
 *  (matching WG's `Rotate_Y` / `Rotate_X` convention). All pitch bones
 *  rotate together — quad turrets get independent control in a future
 *  split. */
const _q = new THREE.Quaternion();
const _y = new THREE.Vector3(0, 1, 0);
const _x = new THREE.Vector3(1, 0, 0);
export function applyAim(rig: TurretRig, yawRad: number, pitchRad: number): void {
  if (rig.yaw && rig.yawRest) {
    _q.setFromAxisAngle(_y, yawRad);
    rig.yaw.quaternion.multiplyQuaternions(rig.yawRest, _q);
  }
  if (rig.pitch.length > 0) {
    _q.setFromAxisAngle(_x, pitchRad);
    for (let i = 0; i < rig.pitch.length; i++) {
      const rest = rig.pitchRest[i];
      if (!rest) continue;
      rig.pitch[i].quaternion.multiplyQuaternions(rest, _q);
    }
  }
}

/** Reset a rig to bind pose. */
export function resetAim(rig: TurretRig): void {
  if (rig.yaw && rig.yawRest) rig.yaw.quaternion.copy(rig.yawRest);
  for (let i = 0; i < rig.pitch.length; i++) {
    const rest = rig.pitchRest[i];
    if (rest) rig.pitch[i].quaternion.copy(rest);
  }
}

/** Section-keyed bucket of rigs. The viewer registers each placed
 *  accessory; the UI reads the bucket to drive turret yaw/pitch
 *  globally or by section ("rotate all main turrets to 30°"). */
export class TurretRigManager {
  /** All rigs keyed by `instance_id`. */
  private rigs = new Map<string, TurretRig>();
  /** Last applied global aim — radians, relative to bind pose. */
  private globalYaw = 0;
  private globalPitch = 0;

  register(rig: TurretRig): void {
    this.rigs.set(rig.instanceId, rig);
    // Apply the current global aim immediately so a freshly-cloned
    // turret matches the existing fleet rather than snapping to rest
    // pose on its first frame.
    if (this.globalYaw !== 0 || this.globalPitch !== 0) {
      applyAim(rig, this.globalYaw, this.globalPitch);
    }
  }

  clear(): void {
    this.rigs.clear();
    this.globalYaw = 0;
    this.globalPitch = 0;
  }

  /** True when at least one placement has a rig. The UI hides the aim
   *  controls otherwise. */
  hasAny(): boolean {
    return this.rigs.size > 0;
  }

  /** Diagnostic: number of placements per section, splitting yaw-only
   *  vs yaw+pitch rigs. */
  size(): number {
    return this.rigs.size;
  }

  /** Drive every rig to the same yaw/pitch (radians). Suitable for the
   *  global slider UI. Future: per-target aim mode would set each rig
   *  independently. */
  setGlobalAim(yawRad: number, pitchRad: number): void {
    this.globalYaw = yawRad;
    this.globalPitch = pitchRad;
    for (const rig of this.rigs.values()) {
      applyAim(rig, yawRad, pitchRad);
    }
  }

  getGlobalAim(): { yaw: number; pitch: number } {
    return { yaw: this.globalYaw, pitch: this.globalPitch };
  }

  /** Snap every rig back to bind pose. */
  reset(): void {
    this.globalYaw = 0;
    this.globalPitch = 0;
    for (const rig of this.rigs.values()) {
      resetAim(rig);
    }
  }
}
