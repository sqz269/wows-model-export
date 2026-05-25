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

/** Per-mount firing-arc limits, resolved from the sidecar mount entry
 *  (GameParams `horizSector` / `vertSector` / `deadZone`). All angles in
 *  DEGREES, in the mount's rest-relative frame (0° = bind/rest heading, the
 *  same frame `applyAim` rotates in). `null`/absent fields mean "no limit"
 *  (free rotation), matching the pre-clamp behaviour for AA + unrigged
 *  mounts. */
export interface MountArcLimits {
  /** `[min, max]` yaw traverse, degrees. Absent = free yaw. */
  yawRangeDeg?: [number, number];
  /** `[depression, elevation]` degrees; positive = up. Absent = free pitch. */
  elevRangeDeg?: [number, number];
  /** `[[start, end], …]` no-fire wedges inside the yaw range — the mount can
   *  rotate here but won't fire (points at its own ship). Visualised by the
   *  firing-arc fan; NOT a rotation clamp. */
  yawDeadZonesDeg?: [number, number][];
}

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
  /** Per-mount firing-arc limits from the sidecar (`null` = no clamp). Yaw
   *  is clamped to `yawRangeDeg`, pitch to `elevRangeDeg`, in `applyAim`. */
  limits: MountArcLimits | null;
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
  limits: MountArcLimits | null = null,
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
    limits,
    nodes,
    boneFrameFix,
  };
}

const _DEG = Math.PI / 180;

/** Clamp a radian value to a degree `[min, max]` range (order-agnostic).
 *  Returns the input unchanged when the range is absent — preserving the
 *  pre-clamp free-rotation behaviour for mounts without arc data. */
function clampRadToDegRange(
  valRad: number,
  rangeDeg: [number, number] | undefined,
): number {
  if (!rangeDeg || rangeDeg.length !== 2) return valRad;
  const lo = Math.min(rangeDeg[0], rangeDeg[1]) * _DEG;
  const hi = Math.max(rangeDeg[0], rangeDeg[1]) * _DEG;
  return Math.max(lo, Math.min(hi, valRad));
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
  // Clamp to the mount's traverse / elevation limits when known. `horizSector`
  // / `vertSector` are rest-relative (0 = bind), the same frame these
  // rotations use, so the clamp is a direct min/max. Dead zones are NOT
  // clamped here — the mount can rotate into them, it just can't fire (the
  // firing-arc fan shades them).
  const yaw = clampRadToDegRange(yawRad, rig.limits?.yawRangeDeg);
  const pitch = clampRadToDegRange(pitchRad, rig.limits?.elevRangeDeg);
  if (rig.yaw && rig.yawRest) {
    _q.setFromAxisAngle(_y, yaw);
    rig.yaw.quaternion.multiplyQuaternions(rig.yawRest, _q);
  }
  if (rig.pitch.length > 0) {
    _q.setFromAxisAngle(_x, pitch * rig.pitchSign);
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

/** Build a flat firing-arc fan for a rigged mount, laid in the horizontal
 *  plane at the yaw pivot. Green = can fire; red = no-fire dead zone (the
 *  mount can rotate there but won't shoot — it's pointing at its own ship).
 *  Returns `null` when the mount has no yaw bone or no `yawRangeDeg`.
 *
 *  Robust to barrel-forward authoring (±Z) and rotation sense: rather than
 *  assume a local axis, it SAMPLES the real barrel-tip direction across the
 *  traverse range by briefly posing the yaw bone (then restoring it), so the
 *  fan always points where the gun actually trains. Built in the yaw bone's
 *  PARENT-local frame; add it to that parent. Fan radius ≈ barrel length. */
export function buildFiringArcFan(rig: TurretRig): THREE.Mesh | null {
  const yawBone = rig.yaw;
  const range = rig.limits?.yawRangeDeg;
  if (!yawBone || !yawBone.parent || !range || range.length !== 2) return null;
  const parent = yawBone.parent;
  const yawRest = rig.yawRest ?? new THREE.Quaternion();
  const d0 = Math.min(range[0], range[1]);
  const d1 = Math.max(range[0], range[1]);
  if (d1 - d0 < 0.5) return null;
  const dead = rig.limits?.yawDeadZonesDeg ?? [];

  // Reference "tip" whose offset from the pivot reveals the barrel bearing.
  const tip =
    rig.nodes.get('HP_gunFire1') ??
    rig.nodes.get('HP_gunFire') ??
    rig.pitch[0] ??
    null;

  const savedYaw = yawBone.quaternion.clone();
  const root = rig.root;
  const axisY = new THREE.Vector3(0, 1, 0);
  const ry = new THREE.Quaternion();
  const pivotL = new THREE.Vector3();
  const tipL = new THREE.Vector3();
  const dir = new THREE.Vector3();
  const N = Math.min(180, Math.max(8, Math.round((d1 - d0) / 2)));
  const rim: { x: number; z: number; deg: number }[] = [];
  let radius = 0;

  try {
    for (let i = 0; i <= N; i++) {
      const deg = d0 + ((d1 - d0) * i) / N;
      ry.setFromAxisAngle(axisY, deg * _DEG);
      yawBone.quaternion.multiplyQuaternions(yawRest, ry);
      root.updateMatrixWorld(true);
      pivotL.setFromMatrixPosition(yawBone.matrixWorld);
      parent.worldToLocal(pivotL);
      if (tip) {
        tipL.setFromMatrixPosition(tip.matrixWorld);
        parent.worldToLocal(tipL);
        dir.subVectors(tipL, pivotL);
      } else {
        dir.set(0, 0, -1).applyQuaternion(yawBone.quaternion);
      }
      dir.y = 0; // flatten to horizontal (mount parent ≈ ship-upright)
      const len = dir.length();
      if (len > radius) radius = len;
      dir.normalize();
      rim.push({ x: dir.x, z: dir.z, deg });
    }
  } finally {
    yawBone.quaternion.copy(savedYaw);
    root.updateMatrixWorld(true);
  }
  if (radius < 1e-3) radius = 5; // degenerate (no usable tip offset)

  // Pivot is constant across the sweep — use the bone's parent-local position.
  const center = { x: yawBone.position.x, y: yawBone.position.y, z: yawBone.position.z };
  return fanMeshFromRim(center, rim, radius, dead);
}

/** Shared wedge-mesh builder: a flat triangle fan from `center` out to
 *  `radius`, one slice per `rim` entry, coloured green (can fire) or red
 *  (no-fire dead zone) per `dead`. `rim` holds unit directions in the XZ
 *  plane tagged with their degree angle. */
function fanMeshFromRim(
  center: { x: number; y: number; z: number },
  rim: { x: number; z: number; deg: number }[],
  radius: number,
  dead: [number, number][],
): THREE.Mesh {
  const positions: number[] = [];
  const colors: number[] = [];
  const GREEN: [number, number, number] = [0.25, 0.9, 0.35];
  const RED: [number, number, number] = [0.95, 0.25, 0.2];
  const inDead = (deg: number) =>
    dead.some((dz) => deg >= Math.min(dz[0], dz[1]) && deg <= Math.max(dz[0], dz[1]));
  for (let i = 0; i < rim.length - 1; i++) {
    const a = rim[i];
    const b = rim[i + 1];
    const c = inDead((a.deg + b.deg) / 2) ? RED : GREEN;
    positions.push(center.x, center.y, center.z);
    positions.push(center.x + a.x * radius, center.y, center.z + a.z * radius);
    positions.push(center.x + b.x * radius, center.y, center.z + b.z * radius);
    for (let k = 0; k < 3; k++) colors.push(c[0], c[1], c[2]);
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geom.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  const mat = new THREE.MeshBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.3,
    side: THREE.DoubleSide,
    depthWrite: false,
  });
  const mesh = new THREE.Mesh(geom, mat);
  mesh.name = '__firingArcFan';
  mesh.userData.firingArcFan = true;
  mesh.renderOrder = 998;
  return mesh;
}

/** Build a firing-arc fan for a STATIC mount that has no rig bone — torpedo
 *  tubes are static meshes the engine rotates wholesale, so they carry a
 *  `yawRangeDeg` but no `Rotate_Y` to sample. The fan is built in the cloned
 *  instance's local frame (0° = local −Z, the authored launch/forward axis;
 *  WG tubes extend along −Z) and added to that instance, so the placement
 *  matrix orients it into ship space. Visualisation only — the mesh doesn't
 *  animate. `root` must already be in the scene graph (matrices current). */
export function buildStaticArcFan(
  root: THREE.Object3D,
  limits: MountArcLimits,
): THREE.Mesh | null {
  const range = limits.yawRangeDeg;
  if (!range || range.length !== 2) return null;
  const d0 = Math.min(range[0], range[1]);
  const d1 = Math.max(range[0], range[1]);
  if (d1 - d0 < 0.5) return null;
  const dead = limits.yawDeadZonesDeg ?? [];

  // Local-frame bbox (geometry extents relative to `root`) → fan radius +
  // height. Local (not world) so the radius is in the same units the fan
  // mesh lives in regardless of the placement's scale.
  root.updateWorldMatrix(true, false);
  const rootInv = new THREE.Matrix4().copy(root.matrixWorld).invert();
  const box = new THREE.Box3();
  const rel = new THREE.Matrix4();
  const tmp = new THREE.Box3();
  root.traverse((o) => {
    const m = o as THREE.Mesh;
    if (!m.isMesh || !m.geometry) return;
    if (!m.geometry.boundingBox) m.geometry.computeBoundingBox();
    const bb = m.geometry.boundingBox;
    if (!bb) return;
    o.updateWorldMatrix(true, false);
    rel.copy(rootInv).multiply(o.matrixWorld);
    tmp.copy(bb).applyMatrix4(rel);
    box.union(tmp);
  });
  let radius = 5;
  let baseY = 0;
  if (!box.isEmpty()) {
    const size = box.getSize(new THREE.Vector3());
    radius = 0.6 * Math.max(size.x, size.z, size.y);
    baseY = (box.min.y + box.max.y) / 2;
  }

  const N = Math.min(180, Math.max(8, Math.round((d1 - d0) / 2)));
  const rim: { x: number; z: number; deg: number }[] = [];
  for (let i = 0; i <= N; i++) {
    const deg = d0 + ((d1 - d0) * i) / N;
    const t = deg * _DEG;
    // 0° = −Z; +deg rotates toward −X (matches the +Y rotation sense the
    // rigged fan uses, so both look consistent).
    rim.push({ x: -Math.sin(t), z: -Math.cos(t), deg });
  }
  return fanMeshFromRim({ x: 0, y: baseY, z: 0 }, rim, radius, dead);
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
  /** Lazily-built firing-arc fan meshes, keyed by instance_id. Visibility
   *  is a cross-ship UI preference (`fansVisible`); the meshes themselves
   *  are per-ship and disposed on `clear()`. */
  private fans = new Map<string, THREE.Mesh>();
  private fansVisible = false;
  /** Static (non-rigged) mounts that still carry a traverse arc — torpedo
   *  tubes are static meshes the engine rotates wholesale, so they have a
   *  `yawRangeDeg` but no `Rotate_Y` bone and thus no rig. We can't animate
   *  them, but we can draw their firing arc. Keyed by instance_id; disjoint
   *  from `rigs` (a mount is one or the other). */
  private staticArcs = new Map<string, { root: THREE.Object3D; limits: MountArcLimits }>();

  register(rig: TurretRig): void {
    this.rigs.set(rig.instanceId, rig);
    // Apply the current global aim immediately so a freshly-cloned
    // turret matches the existing fleet rather than snapping to rest
    // pose on its first frame.
    if (this.globalYaw !== 0 || this.globalPitch !== 0) {
      applyAim(rig, this.globalYaw, this.globalPitch);
    }
    if (this.fansVisible) this.ensureFan(rig);
  }

  /** Register a static (non-rigged) mount's traverse arc — torpedo tubes,
   *  etc. The mount can't be animated, but its firing arc is drawn when the
   *  fans are shown. */
  registerStaticArc(instanceId: string, root: THREE.Object3D, limits: MountArcLimits): void {
    if (!limits.yawRangeDeg) return;
    this.staticArcs.set(instanceId, { root, limits });
    if (this.fansVisible) this.ensureStaticFan(instanceId);
  }

  /** Build + attach a fan for one rig if it doesn't have one yet. */
  private ensureFan(rig: TurretRig): void {
    if (this.fans.has(rig.instanceId)) return;
    const fan = buildFiringArcFan(rig);
    if (fan && rig.yaw?.parent) {
      fan.visible = this.fansVisible;
      rig.yaw.parent.add(fan);
      this.fans.set(rig.instanceId, fan);
    }
  }

  /** Build + attach a placement-based fan for one static mount. */
  private ensureStaticFan(instanceId: string): void {
    if (this.fans.has(instanceId)) return;
    const entry = this.staticArcs.get(instanceId);
    if (!entry) return;
    const fan = buildStaticArcFan(entry.root, entry.limits);
    if (fan) {
      fan.visible = this.fansVisible;
      entry.root.add(fan);
      this.fans.set(instanceId, fan);
    }
  }

  private disposeFans(): void {
    for (const fan of this.fans.values()) {
      fan.parent?.remove(fan);
      fan.geometry.dispose();
      const m = fan.material;
      (Array.isArray(m) ? m : [m]).forEach((x) => x.dispose());
    }
    this.fans.clear();
  }

  clear(): void {
    this.disposeFans();
    this.rigs.clear();
    this.staticArcs.clear();
    this.globalYaw = 0;
    this.globalPitch = 0;
    // `fansVisible` is a UI preference — preserved across ship swaps so the
    // next ship's rigs rebuild their fans on register().
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

  /** Toggle the per-mount firing-arc fans (green = can fire, red = no-fire
   *  dead zone). Fans are built lazily on first show. */
  setFiringArcsVisible(visible: boolean): void {
    this.fansVisible = visible;
    if (visible) {
      for (const rig of this.rigs.values()) this.ensureFan(rig);
      for (const id of this.staticArcs.keys()) this.ensureStaticFan(id);
    }
    for (const fan of this.fans.values()) fan.visible = visible;
  }

  isFiringArcsVisible(): boolean {
    return this.fansVisible;
  }

  /** Number of mounts that carry a yaw traverse arc — rigged guns plus
   *  static torpedo tubes (drives the firing-arc UI hint + toggle gating). */
  countWithLimits(): number {
    let n = this.staticArcs.size;
    for (const rig of this.rigs.values()) {
      if (rig.limits?.yawRangeDeg) n += 1;
    }
    return n;
  }
}
