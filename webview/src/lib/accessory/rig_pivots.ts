// Rig-pivot overlay: yaw / elev / muzzle-tip / barrel-axis markers from
// the `<asset>.rig_pivots.json` sidecar (`compose.turret_autorig`). All
// coords are in the library GLB frame (metres, right-handed) and apply
// to the Three.js scene AS-IS — unlike Blender, three.js's GLTFLoader
// does not rotate the mesh on import.
//
// Colour key (matches the Blender verification tool):
//   red    — yaw pivot + vertical rotation axis
//   green  — elev/trunnion pivot + elev axis along X
//   amber  — extracted muzzle tip
//   purple — implied barrel axis (trunnion-at-barrel-X → muzzle)

import * as THREE from 'three';

import type { RigPivots } from '$lib/types';

const PIVOT_COLORS = {
  yaw: 0xff2424,
  elev: 0x24ff3f,
  muzzle: 0xffbf1a,
  axis: 0xd94df2,
} as const;

export class RigPivotOverlay {
  /** Single group holding every marker. Adding it to the scene once
   *  means a future `setRigFlip180` rotation hits all markers at once. */
  readonly group: THREE.Group;

  private mats: Record<keyof typeof PIVOT_COLORS, THREE.MeshBasicMaterial>;

  constructor() {
    this.group = new THREE.Group();
    this.group.name = 'RigPivots';
    this.group.visible = false;

    // `depthTest: false` + a high `renderOrder` so markers stay
    // visible through the hull mesh in solid render mode. Matches the
    // legacy overlay's "always on top" behaviour.
    this.mats = {
      yaw: new THREE.MeshBasicMaterial({ color: PIVOT_COLORS.yaw, depthTest: false }),
      elev: new THREE.MeshBasicMaterial({ color: PIVOT_COLORS.elev, depthTest: false }),
      muzzle: new THREE.MeshBasicMaterial({ color: PIVOT_COLORS.muzzle, depthTest: false }),
      axis: new THREE.MeshBasicMaterial({ color: PIVOT_COLORS.axis, depthTest: false }),
    };
  }

  setVisible(show: boolean): void {
    this.group.visible = show;
  }

  /** Rotate the overlay 180° around the yaw axis. Quick A/B for
   *  forward-axis mismatches between the pivots and mesh. Not
   *  persisted — the JSON on disk doesn't change. */
  setFlip180(on: boolean): void {
    this.group.rotation.y = on ? Math.PI : 0;
  }

  /** Rebuild every marker from `pivots`. Pass `null` to clear. Sizes
   *  scale to the loaded mesh's bbox so a 16" gun and a 30 mm AA mount
   *  both get visually-reasonable markers. */
  setPivots(pivots: RigPivots | null, bounds: THREE.Box3): void {
    this.disposeChildren();
    if (!pivots) return;

    const bboxSize = new THREE.Vector3();
    bounds.getSize(bboxSize);
    const bboxDim = Math.max(bboxSize.x, bboxSize.y, bboxSize.z) || 1;
    const r = Math.max(0.06, Math.min(0.8, bboxDim * 0.025));

    const { yaw, elev, muzzle_tips } = pivots.pivots;
    const tips = muzzle_tips.length ? muzzle_tips : (pivots.pivots.muzzle_tips_alt ?? []);

    // Yaw: sphere + vertical axis cylinder spanning the bbox height.
    const yBottom = bounds.min.y - bboxSize.y * 0.05;
    const yTop = bounds.max.y + bboxSize.y * 0.15;
    this.addSphere('yaw', yaw, r * 1.1, this.mats.yaw);
    this.addCylinder(
      'yaw_axis',
      [yaw[0], yBottom, yaw[2]],
      [yaw[0], yTop, yaw[2]],
      r * 0.18,
      this.mats.yaw,
    );

    // Elev: sphere + horizontal axis cylinder along X through the trunnion.
    const maxTipX = tips.reduce((m, t) => Math.max(m, Math.abs(t[0])), 0);
    const elevHalf = Math.max(maxTipX * 1.2, bboxDim * 0.1);
    this.addSphere('elev', elev, r * 1.0, this.mats.elev);
    this.addCylinder(
      'elev_axis',
      [elev[0] - elevHalf, elev[1], elev[2]],
      [elev[0] + elevHalf, elev[1], elev[2]],
      r * 0.18,
      this.mats.elev,
    );

    // Per-barrel: implied barrel axis (purple) + muzzle tip (amber).
    for (let i = 0; i < tips.length; i++) {
      const tip = tips[i];
      const base: [number, number, number] = [tip[0], elev[1], elev[2]];
      this.addCylinder(`barrel_axis_${i}`, base, tip, r * 0.14, this.mats.axis);
      this.addSphere(`base_${i}`, base, r * 0.8, this.mats.axis);
      this.addSphere(`muzzle_${i}`, tip, r * 0.95, this.mats.muzzle);
    }
  }

  dispose(): void {
    this.disposeChildren();
    for (const m of Object.values(this.mats)) m.dispose();
  }

  private disposeChildren(): void {
    for (let i = this.group.children.length - 1; i >= 0; i--) {
      const c = this.group.children[i] as THREE.Mesh;
      this.group.remove(c);
      c.geometry?.dispose();
    }
  }

  private addSphere(
    name: string,
    pos: [number, number, number],
    radius: number,
    mat: THREE.Material,
  ): void {
    const g = new THREE.SphereGeometry(radius, 12, 10);
    const m = new THREE.Mesh(g, mat);
    m.name = name;
    m.position.set(pos[0], pos[1], pos[2]);
    m.renderOrder = 999;
    this.group.add(m);
  }

  private addCylinder(
    name: string,
    p0: [number, number, number],
    p1: [number, number, number],
    radius: number,
    mat: THREE.Material,
  ): void {
    const a = new THREE.Vector3(...p0);
    const b = new THREE.Vector3(...p1);
    const dir = b.clone().sub(a);
    const length = dir.length();
    if (length < 1e-4) return;
    const g = new THREE.CylinderGeometry(radius, radius, length, 10, 1, false);
    const m = new THREE.Mesh(g, mat);
    m.name = name;
    // Cylinder default axis is +Y; rotate to match dir.
    const up = new THREE.Vector3(0, 1, 0);
    m.quaternion.setFromUnitVectors(up, dir.clone().normalize());
    m.position.copy(a.add(dir.multiplyScalar(0.5)));
    m.renderOrder = 999;
    this.group.add(m);
  }
}
