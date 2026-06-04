// NodeOverlay: a ship-wide "WG-authored bones & VFX points" overlay.
//
// Two data sources, unified into one marker cloud + node list:
//
//   1. Accessory nodes — walked live from the composed ship scene graph
//      (`shipRoot`). Every named Object3D in every placed turret /
//      secondary / director / AA / accessory (and their attached
//      children) is captured at its bind-pose WORLD position. This is
//      where the WG rig bones (`Rotate_Y` / `Rotate_X` / `Roll_Back*`),
//      hardpoints (`HP_*`) and muzzle VFX anchors (`HP_gunFire*` /
//      `HP_gunFireEffect`) live — see `accessory/viewer.ts` getBones for
//      the single-asset version of this walk.
//
//   2. Hull VFX points — `EP_Fire_*` / `EP_Death_*` / `EP_WakeTrace*` /
//      flood / smoke / floodlight. These are NOT nodes in the hull GLB;
//      they live in the model's `<segment>_ep.skel_ext` files, which the
//      pipeline resolves (by `Murmur3(EP_name)`) into
//      `sidecar.effects.attachments[].position` — already in hull-GLB
//      metric space. We read those positions directly.
//
// Positions are captured once at `rebuild()` (rest pose). Aiming turrets
// moves the live rig bones but not these markers until `refresh()` is
// called — adequate for an inventory/diagnostic overlay.
//
// Markers render as screen-space points (one THREE.Points per category so
// a category toggle is just `points.visible`), drawn on top of the hull
// (`depthTest: false`) so internal points stay visible. Labels are
// on-demand: a hover sprite (driven by `pickAt` from the viewer's
// pointermove) plus a pinned sprite (from the bottom-panel list).

import * as THREE from 'three';

import type { ParticleAttachment } from '$lib/types/sidecar';

/** Marker category. Drives colour + the per-category visibility toggles. */
export type NodeCategory =
  | 'hullEffect' // EP_* hull VFX points (sidecar-sourced)
  | 'gunfire' // HP_gunFire* / HP_gunFireEffect — muzzle VFX anchors
  | 'hardpoint' // other HP_* attach points
  | 'rig' // Rotate_Y / Rotate_X / Rotate_X1 / Roll_Back* rig bones
  | 'blend' // *_BlendBone + Root / Scene Root structural bones
  | 'mesh' // nodes that carry geometry (off by default)
  | 'other'; // any other named empty (off by default)

/** Ordered for the legend / control panel. */
export const NODE_CATEGORIES: NodeCategory[] = [
  'hullEffect',
  'gunfire',
  'hardpoint',
  'rig',
  'blend',
  'mesh',
  'other',
];

export const NODE_CATEGORY_LABEL: Record<NodeCategory, string> = {
  hullEffect: 'Hull VFX (EP_)',
  gunfire: 'Gun-fire VFX',
  hardpoint: 'Hardpoints',
  rig: 'Rig bones',
  blend: 'Blend / root',
  mesh: 'Mesh nodes',
  other: 'Other named',
};

/** Marker colour per category (hex). */
export const NODE_CATEGORY_COLOR: Record<NodeCategory, number> = {
  hullEffect: 0xff3399, // magenta — the headline hull VFX points
  gunfire: 0xff5a1f, // orange — muzzle blast / effect
  hardpoint: 0x33ccff, // cyan — attach hardpoints
  rig: 0xffd23f, // amber — yaw/pitch/recoil bones
  blend: 0x9b8cff, // violet — blend bones / roots
  mesh: 0x4caf6a, // green — geometry nodes
  other: 0xb0b6c0, // grey — misc
};

/** Categories shown when the overlay is first switched on. */
export const NODE_CATEGORY_DEFAULT_ON: Record<NodeCategory, boolean> = {
  hullEffect: true,
  gunfire: true,
  hardpoint: true,
  rig: true,
  blend: true,
  mesh: false,
  other: false,
};

/** One overlay node — surfaced to the bottom-panel list + used for pins. */
export interface NodeEntry {
  /** glTF node name (`Rotate_Y`, `HP_gunFire1`) or hull EP_ node name. */
  name: string;
  category: NodeCategory;
  /** Bind-pose world position captured at rebuild. */
  position: { x: number; y: number; z: number };
  /** Owning accessory label (asset_id) or `Hull` for EP_ points. */
  owner: string;
  /** Hull section the owner sits in (`turrets`, `secondaries`, …) or
   *  `hull` for EP_ points. */
  section: string;
  /** For hull EP_ points: the effect group (`fire1`, `death`, …). */
  effectGroup?: string;
}

/** Result of a screen-space pick — the entry plus its category. */
export interface NodePickResult {
  entry: NodeEntry;
}

// Name classifiers (first match wins; order matters — gunfire before HP_).
function classify(name: string, isMesh: boolean): NodeCategory {
  if (/^HP_gunFire/i.test(name)) return 'gunfire';
  if (/^HP_/i.test(name)) return 'hardpoint';
  if (/^Rotate_|^Roll_Back/i.test(name)) return 'rig';
  if (/BlendBone$|^Root$|^Root_|^Scene Root$/i.test(name)) return 'blend';
  if (isMesh) return 'mesh';
  return 'other';
}

// Structural container nodes we never surface as markers.
const SKIP_NAMES = new Set(['Ship', 'Hull', '__bonePoints', '__bonePin', '__boneHover']);
function isSkippableName(name: string): boolean {
  return SKIP_NAMES.has(name) || name.startsWith('Section.') || name.startsWith('__');
}

export class NodeOverlay {
  /** Single root group (added to the scene once). */
  readonly group: THREE.Group;

  private categoryPoints = new Map<NodeCategory, THREE.Points>();
  private entries: NodeEntry[] = [];
  /** Per-category flat world-position arrays, index-aligned with the
   *  per-category entry list — for screen-space picking. */
  private byCategory = new Map<NodeCategory, NodeEntry[]>();
  private visible = false;
  private categoryVisible: Record<NodeCategory, boolean> = { ...NODE_CATEGORY_DEFAULT_ON };

  private pointTexture: THREE.Texture;
  private pointSize = 11; // screen-space px (sizeAttenuation off)

  private hoverSprite: THREE.Sprite | null = null;
  private pinSprite: THREE.Sprite | null = null;
  private pinMarker: THREE.Mesh | null = null;
  private pinnedName: string | null = null;

  // Scratch for screen-space picking.
  private _v = new THREE.Vector3();

  constructor() {
    this.group = new THREE.Group();
    this.group.name = '__bonePoints';
    this.group.visible = false;
    this.pointTexture = makeDiscTexture();
  }

  // ── Build / teardown ──────────────────────────────────────────────────

  /**
   * Rebuild every marker from the current ship. `shipRoot` is walked for
   * accessory nodes; `effects` supplies hull EP_ points (those carrying a
   * resolved `position`). Safe to call repeatedly — clears prior markers.
   */
  rebuild(shipRoot: THREE.Object3D, effects: ParticleAttachment[] | null): void {
    this.clearMarkers();
    shipRoot.updateMatrixWorld(true);

    const collected: NodeEntry[] = [];
    const seen = new Set<THREE.Object3D>();

    // 1. Accessory + hull-mesh nodes from the live scene graph.
    shipRoot.traverse((obj) => {
      if (seen.has(obj)) return;
      seen.add(obj);
      const name = obj.name;
      if (!name || isSkippableName(name)) return;
      const isMesh = (obj as THREE.Mesh).isMesh === true;
      const category = classify(name, isMesh);
      const p = obj.getWorldPosition(this._v.clone());
      const { owner, section } = resolveOwner(obj);
      collected.push({
        name,
        category,
        position: { x: p.x, y: p.y, z: p.z },
        owner,
        section,
      });
    });

    // 2. Hull EP_ VFX points from the sidecar (position already in hull
    //    GLB / world space). De-dup by node name — a single EP_ node may be
    //    referenced by several effect groups (fire1 + fire2 + …); one marker.
    if (effects) {
      const epSeen = new Set<string>();
      for (const a of effects) {
        if (!a.position || a.position.length !== 3) continue;
        if (!a.node || epSeen.has(a.node)) continue;
        epSeen.add(a.node);
        collected.push({
          name: a.node,
          category: 'hullEffect',
          position: { x: a.position[0], y: a.position[1], z: a.position[2] },
          owner: 'Hull',
          section: 'hull',
          effectGroup: a.group,
        });
      }
    }

    collected.sort((x, y) => x.name.localeCompare(y.name));
    this.entries = collected;

    // Bucket by category and build one Points per category.
    this.byCategory.clear();
    for (const cat of NODE_CATEGORIES) this.byCategory.set(cat, []);
    for (const e of collected) this.byCategory.get(e.category)!.push(e);

    for (const cat of NODE_CATEGORIES) {
      const list = this.byCategory.get(cat)!;
      if (list.length === 0) continue;
      const positions = new Float32Array(list.length * 3);
      for (let i = 0; i < list.length; i++) {
        positions[i * 3] = list[i].position.x;
        positions[i * 3 + 1] = list[i].position.y;
        positions[i * 3 + 2] = list[i].position.z;
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      const mat = new THREE.PointsMaterial({
        color: NODE_CATEGORY_COLOR[cat],
        size: this.pointSize,
        sizeAttenuation: false,
        map: this.pointTexture,
        alphaTest: 0.5,
        transparent: true,
        depthTest: false,
        depthWrite: false,
      });
      const pts = new THREE.Points(geom, mat);
      pts.name = `__bonePoints_${cat}`;
      pts.renderOrder = 997;
      pts.visible = this.categoryVisible[cat];
      this.group.add(pts);
      this.categoryPoints.set(cat, pts);
    }
  }

  /** Re-read accessory world positions (e.g. after the user aims turrets).
   *  Hull EP_ points are static so they're left untouched. Cheap — just
   *  rewrites the position buffers. Requires the original Object3Ds, so it
   *  re-walks from `shipRoot`. */
  refresh(shipRoot: THREE.Object3D, effects: ParticleAttachment[] | null): void {
    if (this.entries.length === 0) return;
    this.rebuild(shipRoot, effects);
  }

  // ── Visibility ────────────────────────────────────────────────────────

  setVisible(on: boolean): void {
    this.visible = on;
    this.group.visible = on;
    if (!on) {
      this.setHover(null);
    }
  }

  isVisible(): boolean {
    return this.visible;
  }

  setCategoryVisible(cat: NodeCategory, on: boolean): void {
    this.categoryVisible[cat] = on;
    const pts = this.categoryPoints.get(cat);
    if (pts) pts.visible = on;
  }

  getCategoryVisible(cat: NodeCategory): boolean {
    return this.categoryVisible[cat];
  }

  /** Per-category marker counts for the control panel. */
  getCounts(): Record<NodeCategory, number> {
    const out = {} as Record<NodeCategory, number>;
    for (const cat of NODE_CATEGORIES) out[cat] = this.byCategory.get(cat)?.length ?? 0;
    return out;
  }

  /** Full node list for the bottom-panel inventory. */
  getNodes(): readonly NodeEntry[] {
    return this.entries;
  }

  // ── Picking + labels ──────────────────────────────────────────────────

  /**
   * Nearest VISIBLE node to a screen point, within a pixel threshold.
   * Used for hover (and could back click-to-inspect). `rect` is the
   * canvas bounding rect; `clientX/Y` are document coords.
   */
  pickAt(
    clientX: number,
    clientY: number,
    camera: THREE.Camera,
    rect: DOMRect,
    thresholdPx = 12,
  ): NodePickResult | null {
    if (!this.visible) return null;
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    let best: NodeEntry | null = null;
    let bestD2 = thresholdPx * thresholdPx;
    for (const cat of NODE_CATEGORIES) {
      if (!this.categoryVisible[cat]) continue;
      const list = this.byCategory.get(cat);
      if (!list) continue;
      for (const e of list) {
        this._v.set(e.position.x, e.position.y, e.position.z).project(camera);
        if (this._v.z < -1 || this._v.z > 1) continue; // behind / clipped
        const sx = ((this._v.x + 1) / 2) * rect.width;
        const sy = ((1 - this._v.y) / 2) * rect.height;
        const dx = sx - px;
        const dy = sy - py;
        const d2 = dx * dx + dy * dy;
        if (d2 < bestD2) {
          bestD2 = d2;
          best = e;
        }
      }
    }
    return best ? { entry: best } : null;
  }

  /** Show / move the transient hover label, or hide it (null). */
  setHover(entry: NodeEntry | null): void {
    if (!entry) {
      if (this.hoverSprite) this.hoverSprite.visible = false;
      return;
    }
    if (!this.hoverSprite) {
      this.hoverSprite = makeLabelSprite('');
      this.hoverSprite.renderOrder = 1000;
      this.group.add(this.hoverSprite);
    }
    setLabelSpriteText(this.hoverSprite, entry.name);
    this.hoverSprite.position.set(entry.position.x, entry.position.y, entry.position.z);
    this.hoverSprite.visible = true;
  }

  /** Pin a marker + persistent label at the named node (or clear with
   *  null). Returns the entry so the caller can frame the camera on it. */
  pin(name: string | null): NodeEntry | null {
    if (!name) {
      if (this.pinSprite) this.pinSprite.visible = false;
      if (this.pinMarker) this.pinMarker.visible = false;
      this.pinnedName = null;
      return null;
    }
    const entry = this.entries.find((e) => e.name === name);
    if (!entry) return null;
    this.pinnedName = name;
    if (!this.pinMarker) {
      const geom = new THREE.SphereGeometry(1, 16, 12);
      const mat = new THREE.MeshBasicMaterial({
        color: 0xffffff,
        depthTest: false,
        transparent: true,
        opacity: 0.9,
      });
      this.pinMarker = new THREE.Mesh(geom, mat);
      this.pinMarker.renderOrder = 999;
      this.group.add(this.pinMarker);
    }
    (this.pinMarker.material as THREE.MeshBasicMaterial).color.setHex(
      NODE_CATEGORY_COLOR[entry.category],
    );
    this.pinMarker.position.set(entry.position.x, entry.position.y, entry.position.z);
    this.pinMarker.visible = true;
    if (!this.pinSprite) {
      this.pinSprite = makeLabelSprite('');
      this.pinSprite.renderOrder = 1001;
      this.group.add(this.pinSprite);
    }
    setLabelSpriteText(this.pinSprite, entry.name);
    this.pinSprite.position.set(entry.position.x, entry.position.y, entry.position.z);
    this.pinSprite.visible = true;
    return entry;
  }

  getPinned(): string | null {
    return this.pinnedName;
  }

  // ── Teardown ──────────────────────────────────────────────────────────

  /** Drop every marker (used on ship swap before the next rebuild). */
  clear(): void {
    this.clearMarkers();
  }

  private clearMarkers(): void {
    for (const pts of this.categoryPoints.values()) {
      this.group.remove(pts);
      pts.geometry.dispose();
      (pts.material as THREE.Material).dispose();
    }
    this.categoryPoints.clear();
    this.byCategory.clear();
    this.entries = [];
    this.pin(null);
    this.setHover(null);
  }

  dispose(): void {
    this.clearMarkers();
    if (this.hoverSprite) disposeSprite(this.hoverSprite);
    if (this.pinSprite) disposeSprite(this.pinSprite);
    if (this.pinMarker) {
      this.pinMarker.geometry.dispose();
      (this.pinMarker.material as THREE.Material).dispose();
    }
    this.hoverSprite = null;
    this.pinSprite = null;
    this.pinMarker = null;
    this.pointTexture.dispose();
  }
}

// ── Free helpers ──────────────────────────────────────────────────────────

/** Walk up to the owning accessory (userData.asset_id /
 *  attached_asset_id) and section, for list grouping. Falls back to
 *  `Hull` / `hull` for nodes under the hull group. */
function resolveOwner(obj: THREE.Object3D): { owner: string; section: string } {
  let n: THREE.Object3D | null = obj;
  while (n) {
    const ud = n.userData;
    if (ud) {
      const aid = ud.attached_asset_id ?? ud.asset_id;
      if (typeof aid === 'string') {
        const section = typeof ud.section === 'string' ? ud.section : 'accessory';
        return { owner: aid, section };
      }
    }
    if (n.name === 'Hull') return { owner: 'Hull', section: 'hull' };
    n = n.parent;
  }
  return { owner: '(scene)', section: 'scene' };
}

/** A soft round dot texture for the point markers (a hard square reads as
 *  noise at ship scale). */
function makeDiscTexture(): THREE.Texture {
  const s = 64;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const ctx = c.getContext('2d')!;
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.6, 'rgba(255,255,255,1)');
  g.addColorStop(0.7, 'rgba(255,255,255,0.6)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, s, s);
  const tex = new THREE.CanvasTexture(c);
  tex.needsUpdate = true;
  return tex;
}

/** Build a text-label sprite (white text on a translucent dark pill).
 *  Sized so it stays readable; `depthTest: false` keeps it on top. */
function makeLabelSprite(text: string): THREE.Sprite {
  const mat = new THREE.SpriteMaterial({
    depthTest: false,
    depthWrite: false,
    transparent: true,
  });
  const sprite = new THREE.Sprite(mat);
  setLabelSpriteText(sprite, text);
  return sprite;
}

function setLabelSpriteText(sprite: THREE.Sprite, text: string): void {
  const font = 'bold 28px ui-monospace, monospace';
  const pad = 12;
  const measure = document.createElement('canvas').getContext('2d')!;
  measure.font = font;
  const w = Math.ceil(measure.measureText(text).width) + pad * 2;
  const h = 44;
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  const ctx = c.getContext('2d')!;
  ctx.fillStyle = 'rgba(10,12,17,0.82)';
  roundRect(ctx, 0, 0, w, h, 8);
  ctx.fill();
  ctx.font = font;
  ctx.fillStyle = '#fff';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, pad, h / 2 + 1);
  const tex = new THREE.CanvasTexture(c);
  tex.needsUpdate = true;
  const mat = sprite.material as THREE.SpriteMaterial;
  mat.map?.dispose();
  mat.map = tex;
  mat.needsUpdate = true;
  // Keep a constant on-screen-ish size: scale in world units, tuned for
  // the ~300 m ship scene. Aspect from the canvas so text isn't stretched.
  const scale = 0.06;
  sprite.scale.set(w * scale, h * scale, 1);
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function disposeSprite(sprite: THREE.Sprite): void {
  const mat = sprite.material as THREE.SpriteMaterial;
  mat.map?.dispose();
  mat.dispose();
}
