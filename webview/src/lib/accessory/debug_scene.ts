// Rig debug-scene support: per-piece tracking, category recolor, raycast
// picking. Used by the rig editor UI in `routes/Library` → AssetDetail.
//
// A debug scene is a special `<asset>.rig.debug.glb` produced by the
// (legacy) `turret_rig.py --debug-scene` path. Each loose-parts piece
// is its own mesh with category extras baked into glTF node `extras`:
//
//   wows_rig_fingerprint_center: [x, y, z]  (bbox centre, metres)
//   wows_rig_fingerprint_verts:  int        (vertex count)
//   wows_rig_category:           "body" | "elev" | "skin"
//   wows_rig_face_plate:         boolean    (auto face-plate flag)
//
// The fingerprint matches the override schema consumed by the Python
// rigger (`RigOverrides`). Pieces are addressed by index into the
// loaded scene's tracked-piece array, which stays stable for the
// lifetime of the load — the override-loader resolves index ↔
// fingerprint on save and on rebuild-reload.

import * as THREE from 'three';

import type { PieceInfo, RigCategory } from '$lib/types';

// Category → debug-scene base colour. Mirrors
// `turret_rig_blender._DebugCat_*` so the in-browser colour-coding
// stays in sync with what the rigger emits.
const RIG_CATEGORY_COLORS: Record<RigCategory | 'face', [number, number, number]> = {
  body: [0.85, 0.20, 0.20],
  elev: [0.20, 0.85, 0.30],
  skin: [0.25, 0.45, 0.95],
  face: [0.95, 0.85, 0.10],
};

interface DebugPieceTracking {
  mesh: THREE.Mesh;
  info: PieceInfo;
  /** Snapshot of the original material so a `setPieceCategoryColor(i,
   *  'auto')` revert lands on the rigger-emitted colour. */
  autoMaterial: THREE.Material;
}

export interface DebugSceneLoadResult {
  bounds: THREE.Box3;
  pieces: PieceInfo[];
}

/** Picker materials are shared across pieces — recoloring N pieces to
 *  the same category reuses one material rather than allocating N. */
function buildPickerMaterials(): Record<string, THREE.MeshStandardMaterial> {
  const out: Record<string, THREE.MeshStandardMaterial> = {};
  for (const [cat, [r, g, b]] of Object.entries(RIG_CATEGORY_COLORS)) {
    out[cat] = new THREE.MeshStandardMaterial({
      color: new THREE.Color(r, g, b),
      roughness: 0.8,
      metalness: 0.0,
    });
  }
  return out;
}

/** Read the rigger-baked extras off a glTF node. Returns `null` for
 *  any node missing or with malformed extras (rebuild-mid-pick race,
 *  malformed input). */
function readPieceExtras(node: THREE.Object3D): Omit<PieceInfo, 'index'> | null {
  const extras = (node.userData as Record<string, unknown>) || {};
  const center = extras['wows_rig_fingerprint_center'];
  const verts = extras['wows_rig_fingerprint_verts'];
  const cat = extras['wows_rig_category'];
  const isFace = extras['wows_rig_face_plate'];
  if (!Array.isArray(center) || center.length !== 3) return null;
  if (typeof verts !== 'number') return null;
  if (cat !== 'body' && cat !== 'elev' && cat !== 'skin') return null;
  return {
    name: node.name || '(unnamed)',
    autoCategory: cat as RigCategory,
    autoFacePlate: Boolean(isFace),
    fingerprint: {
      center: [Number(center[0]), Number(center[1]), Number(center[2])],
      verts: verts,
    },
  };
}

export class DebugSceneController {
  /** Picker materials cached at construction; shared across loads. */
  private readonly pickerMaterials = buildPickerMaterials();
  private pieces: DebugPieceTracking[] = [];

  private pickerEnabled = false;
  private pickHandler: ((piece: PieceInfo) => void) | null = null;

  private pointerDownAt: { x: number; y: number } | null = null;
  private readonly raycaster = new THREE.Raycaster();
  private readonly ndc = new THREE.Vector2();

  // Listeners are attached lazily in `attach()` so the same controller
  // works for a viewer whose container may not exist at construction.
  private detachListeners: (() => void) | null = null;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly camera: THREE.Camera,
  ) {}

  /** Hook pointerdown/up on the canvas. Idempotent. */
  attach(): void {
    if (this.detachListeners) return;
    const onDown = (ev: PointerEvent) => {
      if (!this.pickerEnabled) return;
      this.pointerDownAt = { x: ev.clientX, y: ev.clientY };
    };
    const onUp = (ev: PointerEvent) => {
      if (!this.pickerEnabled || !this.pointerDownAt) return;
      const dx = ev.clientX - this.pointerDownAt.x;
      const dy = ev.clientY - this.pointerDownAt.y;
      this.pointerDownAt = null;
      // Drag threshold — anything bigger than 4 px is an orbit, not
      // a pick. Same threshold the legacy viewer used.
      if (Math.hypot(dx, dy) > 4) return;
      if (!this.pieces.length) return;
      const rect = this.canvas.getBoundingClientRect();
      this.ndc.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      this.ndc.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
      this.raycaster.setFromCamera(this.ndc, this.camera);
      const hits = this.raycaster.intersectObjects(
        this.pieces.map((t) => t.mesh),
        false,
      );
      if (!hits.length) return;
      const hit = hits[0];
      const t = this.pieces.find((p) => p.mesh === hit.object);
      if (!t) return;
      this.pickHandler?.(t.info);
    };
    this.canvas.addEventListener('pointerdown', onDown);
    this.canvas.addEventListener('pointerup', onUp);
    this.detachListeners = () => {
      this.canvas.removeEventListener('pointerdown', onDown);
      this.canvas.removeEventListener('pointerup', onUp);
    };
  }

  /** Reset the tracked pieces from a freshly-loaded debug-scene root.
   *  Returns the per-piece info the caller surfaces to the rig editor. */
  ingestRoot(root: THREE.Object3D): PieceInfo[] {
    this.disposePieces();
    let i = 0;
    root.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const info = readPieceExtras(mesh);
      if (!info) return;
      const indexed: PieceInfo = { ...info, index: i++ };
      const autoMat = (
        Array.isArray(mesh.material) ? mesh.material[0] : mesh.material
      ) as THREE.Material;
      this.pieces.push({ mesh, info: indexed, autoMaterial: autoMat });
    });
    return this.pieces.map((t) => t.info);
  }

  setPickerMode(enabled: boolean): void {
    this.pickerEnabled = enabled;
    this.canvas.style.cursor = enabled ? 'crosshair' : '';
  }

  onPiecePicked(handler: ((piece: PieceInfo) => void) | null): void {
    this.pickHandler = handler;
  }

  /** Swap a piece's material between the auto colour and the override
   *  colour for `category`. `'auto'` reverts to whatever the rigger
   *  baked in (the original material). */
  setPieceCategoryColor(idx: number, category: RigCategory | 'face' | 'auto'): void {
    const t = this.pieces[idx];
    if (!t) return;
    if (category === 'auto') {
      t.mesh.material = t.autoMaterial;
      return;
    }
    const mat = this.pickerMaterials[category];
    if (mat) t.mesh.material = mat;
  }

  /** Highlight one piece with a bright emissive clone, dropping any
   *  prior selection. `null` clears the highlight entirely. */
  setSelectedPiece(idx: number | null): void {
    for (const t of this.pieces) {
      const mat = t.mesh.material as THREE.Material | undefined;
      const tag = (mat?.userData as Record<string, unknown> | undefined)?.['__selected'];
      if (tag) {
        (mat as THREE.Material).dispose();
        t.mesh.material = t.autoMaterial;
      }
    }
    if (idx === null) return;
    const sel = this.pieces[idx];
    if (!sel) return;
    const baseMat = sel.mesh.material as THREE.MeshStandardMaterial;
    const high = baseMat.clone();
    high.emissive = new THREE.Color(0.4, 0.4, 0.0);
    high.emissiveIntensity = 1.0;
    high.userData.__selected = true;
    sel.mesh.material = high;
  }

  /** Drop tracked pieces. Disposes the per-piece `autoMaterial` —
   *  these come from the glTF loader and aren't shared anywhere else
   *  in the viewer. */
  disposePieces(): void {
    for (const t of this.pieces) {
      t.mesh.geometry?.dispose();
      t.autoMaterial?.dispose();
    }
    this.pieces = [];
  }

  dispose(): void {
    this.disposePieces();
    for (const m of Object.values(this.pickerMaterials)) m.dispose();
    this.detachListeners?.();
    this.detachListeners = null;
  }
}
