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

import { maybeApplyBoneFrameFix, type BoneFrameDiagnostic } from './bone_frame_fix';

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
  /** Every named node under `root`, indexed by glTF node name. Lets
   *  callers grab a bone we don't have a typed handle for yet:
   *
   *    rig.nodes.get('Roll_Back1')        // recoil bone parent
   *    rig.nodes.get('HP_gunFire1')       // muzzle-tip hardpoint
   *    rig.nodes.get('Root_BlendBone')    // body baseline blend bone
   *
   *  Whatever the WG `.visual` declares is here — see
   *  `reference/topics/turret/turret_skin_pipeline.md` for the
   *  canonical bone-name list. Adding a new feature against a known
   *  bone shouldn't need a change to `extractTurretRig`. */
  nodes: Map<string, THREE.Object3D>;
  /** ±1 — `applyAim` multiplies pitch angle by this before rotating,
   *  so positive UI values always elevate. Pitch propagates from
   *  `Rotate_X` down to the skin palette through the chain that sits
   *  between them, and any Y180-equivalent transform in that chain
   *  conjugates `Rx(θ)` into `Rx(-θ)` — observed on AGS145 US BB
   *  secondaries, where the raw +X rotation depresses instead of
   *  elevating. Detected at extract time by sampling the
   *  most-pitch-weighted skinned vertex's world-Y delta under a small
   *  +X test rotation. Restores WG's `vertSector[1] > 0 = up` contract
   *  (universal across the 1758 Gun entries in GameParams). */
  pitchSign: 1 | -1;
  /** Result of the bone-frame-mismatch fix (see `bone_frame_fix.ts`).
   *  `applied=true` means the asset's bone tree was Y180-wrapped at
   *  clone time because the WG `.geometry` and `.visual` were authored
   *  in 180°-rotated coordinate frames (AGS145 mantlet quirk). Exposed
   *  here so debug UI can surface the diagnostic without re-running the
   *  probe — none of the rig consumers branch on it today. */
  boneFrameFix: BoneFrameDiagnostic;
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

/** Determine whether a positive +X rotation on `pitchBone` actually
 *  elevates the skinned geometry, or depresses it. Returns +1 for
 *  elevate, -1 for depress; defaults to +1 when no representative
 *  pitch-influenced vertex is found (no SkinnedMesh, no skin joints
 *  descended from `pitchBone`, etc.).
 *
 *  Runs the full Three.js skinning math on the CPU for a single
 *  vertex — the one with the highest summed weight on skin joints
 *  descended from `pitchBone` — across two states (rest, +0.1 rad).
 *  The sign of the world-Y delta is `pitchSign`. Vertex selection
 *  doesn't depend on bone naming, just hierarchy, so the probe stays
 *  valid if the toolkit ever changes the BlendBone naming convention. */
function computePitchSign(
  root: THREE.Object3D,
  pitchBone: THREE.Object3D,
  pitchRest: THREE.Quaternion,
): 1 | -1 {
  type Sample = {
    mesh: THREE.SkinnedMesh;
    vertex: THREE.Vector3;
    indices: [number, number, number, number];
    weights: [number, number, number, number];
  };
  let sample: Sample | null = null;
  root.traverse((o) => {
    if (sample) return;
    const sm = o as THREE.SkinnedMesh;
    if (!sm.isSkinnedMesh) return;
    const validJoints = new Set<number>();
    for (let i = 0; i < sm.skeleton.bones.length; i++) {
      let cur: THREE.Object3D | null = sm.skeleton.bones[i];
      while (cur) {
        if (cur === pitchBone) { validJoints.add(i); break; }
        cur = cur.parent;
      }
    }
    if (validJoints.size === 0) return;
    const pos = sm.geometry.attributes.position;
    const ji = sm.geometry.attributes.skinIndex;
    const jw = sm.geometry.attributes.skinWeight;
    if (!pos || !ji || !jw) return;
    let bestSum = 0;
    let bestIdx = -1;
    for (let v = 0; v < pos.count; v++) {
      let sumW = 0;
      for (let k = 0; k < 4; k++) {
        if (validJoints.has(ji.getComponent(v, k))) {
          sumW += jw.getComponent(v, k);
        }
      }
      if (sumW > bestSum) {
        bestSum = sumW;
        bestIdx = v;
      }
    }
    if (bestIdx < 0) return;
    sample = {
      mesh: sm,
      vertex: new THREE.Vector3().fromBufferAttribute(pos, bestIdx),
      indices: [
        ji.getComponent(bestIdx, 0),
        ji.getComponent(bestIdx, 1),
        ji.getComponent(bestIdx, 2),
        ji.getComponent(bestIdx, 3),
      ],
      weights: [
        jw.getComponent(bestIdx, 0),
        jw.getComponent(bestIdx, 1),
        jw.getComponent(bestIdx, 2),
        jw.getComponent(bestIdx, 3),
      ],
    };
  });
  if (!sample) return 1;
  // TypeScript's flow analysis doesn't track the closure mutation inside
  // `traverse`, so the local would otherwise narrow to `never` after the
  // null guard — explicit cast keeps the rest of the function readable.
  const s = sample as Sample;
  const tmpVec = new THREE.Vector3();
  const tmpMat = new THREE.Matrix4();
  const skinnedY = (): number => {
    s.mesh.updateMatrixWorld(true);
    let y = 0;
    for (let k = 0; k < 4; k++) {
      const w = s.weights[k];
      if (w <= 0) continue;
      const j = s.indices[k];
      tmpMat.multiplyMatrices(
        s.mesh.skeleton.bones[j].matrixWorld,
        s.mesh.skeleton.boneInverses[j],
      );
      tmpVec.copy(s.vertex).applyMatrix4(tmpMat);
      y += tmpVec.y * w;
    }
    return y;
  };
  pitchBone.quaternion.copy(pitchRest);
  root.updateMatrixWorld(true);
  const yRest = skinnedY();
  const probe = new THREE.Quaternion().setFromAxisAngle(
    new THREE.Vector3(1, 0, 0),
    0.1,
  );
  pitchBone.quaternion.multiplyQuaternions(pitchRest, probe);
  root.updateMatrixWorld(true);
  const yPitch = skinnedY();
  pitchBone.quaternion.copy(pitchRest);
  root.updateMatrixWorld(true);
  return yPitch - yRest >= 0 ? 1 : -1;
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
  // Bone-frame-mismatch fix MUST run before we capture pitchRest or run
  // the pitchSign probe — both observe the bones' current state, and the
  // fix changes the rest orientation (Y180-conjugating subsequent
  // rotations). The fix is a no-op for the 99% of assets that aren't
  // misaligned; cheap to call unconditionally. See bone_frame_fix.ts
  // for the detection threshold + math.
  const boneFrameFix = maybeApplyBoneFrameFix(root);

  let yaw: THREE.Object3D | null = null;
  const pitch: THREE.Object3D[] = [];
  const nodes = new Map<string, THREE.Object3D>();
  root.traverse((o) => {
    if (!o.name) return;
    // First-writer wins: the WG bone tree has unique names within a
    // single visual (verified across 14 assets); if a clone ever
    // produced collisions we'd rather pin the outermost.
    if (!nodes.has(o.name)) nodes.set(o.name, o);
    if (o.name === YAW_BONE_NAME && !yaw) {
      yaw = o;
    } else if (PITCH_BONE_NAMES.includes(o.name)) {
      pitch.push(o);
    }
  });
  if (!yaw && pitch.length === 0) return null;
  const pitchRest = pitch.map((b) => b.quaternion.clone());
  const pitchSign =
    pitch.length > 0 ? computePitchSign(root, pitch[0], pitchRest[0]) : 1;
  return {
    root,
    instanceId,
    assetId,
    yaw,
    pitch,
    yawRest: yaw ? (yaw as THREE.Object3D).quaternion.clone() : null,
    pitchRest,
    pitchSign,
    nodes,
    boneFrameFix,
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
    _q.setFromAxisAngle(_x, pitchRad * rig.pitchSign);
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
