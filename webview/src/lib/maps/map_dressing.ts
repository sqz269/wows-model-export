// Dressings (map_dressing/v1, reference/schemas/MAP_DRESSING.md): extra model
// instances another producer drops into a map folder as
// dressing/<name>.dressing.json, beside the instance records and the assets
// (glTF / GLB) it names. The Maps page draws each ticked dressing over the
// map: one THREE.InstancedMesh per asset mesh and group, and, for a layer of
// kind 'replace', the layer's models in place of the map_scene.json models it
// names (those are hidden while the layer shows).
//
// Frames. Records and model matrices are in the map's scene frame (the frame
// map_scene.json and a client map GLB use). The dressing root is added under
// the map root, so the root's sea-level shift applies to it too.
//
// Paths in the document are relative to the dressing file's folder and stay
// inside the map folder, except an asset's `workspace_uri`: a shared library
// asset (libraries/...) relative to the workspace root, fetched through /repo.
//
// LODs. An asset with `lods` draws, per instance, only the node whose range
// holds the camera's distance to the instance (distances in the asset's
// units, times unit_scale for the scene); past the last range the last level
// stays (the preview does not cull by view distance). `update(camera)`
// re-sorts the instances when the camera moves.
//
// Tint. `assets[].tint` x `layers[].tint` (linear RGB) multiplies the base
// colour of the asset's materials in that layer.
//
// Ground. A layer with `stands_on` (the id of a `replace` layer) stands on
// that layer's models. While that layer is hidden, each of its records is
// moved by its value in `instances.ground_dy_file` (f32 per record, scene
// units), which stands it on the map models the replace layer stands in for;
// so every instance stays on the ground that is drawn.
//
// No Svelte imports: the Maps route owns the lifecycle.

import * as THREE from 'three';
import { GLTFLoader, type GLTF } from 'three/addons/loaders/GLTFLoader.js';

export const MAP_DRESSING_SCHEMA = 'map_dressing/v1';
/** f32 px, py, pz, qx, qy, qz, qw, s */
const RECORD_FLOATS = 8;
const IDENTITY = new THREE.Matrix4();

export interface MapDressingLod {
  /** Name of a node of the asset's scene (its subtree is the level). */
  node: string;
  /** Upper end of the level's range, in the asset's units (x unit_scale for
   *  the scene); the level starts where the previous one ends. */
  max_distance: number;
}

export interface MapDressingAsset {
  id: string;
  /** glTF / GLB, relative to the dressing file's folder. */
  uri?: string;
  /** Shared asset: a glTF / GLB relative to the workspace root (starts with
   *  `libraries/`). Used instead of `uri` when present. */
  workspace_uri?: string;
  /** Asset units -> scene units (1 for an asset already in scene units). */
  unit_scale?: number;
  /** Levels of detail, nearest first. */
  lods?: MapDressingLod[];
  /** Linear RGB multiplier of the asset's base colour. */
  tint?: number[];
  category?: string;
  triangles?: number;
}

export interface MapDressingGroup {
  asset: string;
  count: number;
  /** Offset of the group's first record in the instance file. */
  byte_offset: number;
  triangles?: number;
}

export interface MapDressingModel {
  name?: string;
  /** glTF / GLB, relative to the dressing file's folder. */
  file: string;
  /** 4x4 column-major, model frame -> scene frame; null = identity. */
  matrix?: number[] | null;
}

export interface MapDressingLayer {
  id: string;
  title?: string;
  /** 'scatter' | 'set_piece' (instance groups) | 'replace' (models). */
  kind?: string;
  visible?: boolean;
  groups?: MapDressingGroup[];
  models?: MapDressingModel[];
  /** map_scene.json model names this layer stands in for while it shows. */
  replaces?: string[];
  /** Linear RGB multiplier of the base colour of every asset in the layer. */
  tint?: number[];
  /** Id of a `replace` layer whose models these instances stand on; while
   *  it is hidden the records move by `instances.ground_dy_file`. */
  stands_on?: string;
}

export interface MapDressingDoc {
  schema: string;
  title?: string;
  description?: string;
  instances?: { file: string; record?: string; ground_dy_file?: string };
  assets?: MapDressingAsset[];
  layers?: MapDressingLayer[];
}

/** Validate the parts the renderer reads; throws on anything else. */
export function parseMapDressing(raw: unknown): MapDressingDoc {
  if (!raw || typeof raw !== 'object') throw new Error('not a JSON object');
  const doc = raw as MapDressingDoc;
  if (doc.schema !== MAP_DRESSING_SCHEMA) throw new Error(`not a ${MAP_DRESSING_SCHEMA} document`);
  if (doc.layers !== undefined && !Array.isArray(doc.layers))
    throw new Error('layers is not a list');
  if (doc.assets !== undefined && !Array.isArray(doc.assets))
    throw new Error('assets is not a list');
  return doc;
}

/** One mesh of one LOD level of a group: an InstancedMesh holding the
 *  instances now in that level, and the mesh's frame inside the asset. */
interface LodMesh {
  im: THREE.InstancedMesh;
  local: THREE.Matrix4;
  identity: boolean;
  triangles: number;
}

interface LodLevel {
  meshes: LodMesh[];
  /** Squared upper end of the level's range, in scene units (Infinity = none). */
  maxDist2: number;
  triangles: number;
}

/** One instance group of a layer, with its levels. */
interface BuiltGroup {
  count: number;
  /** Scene-frame positions (x, y, z per instance). */
  positions: Float32Array;
  /** Instance matrices T(p) R(q) S(s x unit_scale), 16 floats each. */
  matrices: Float32Array;
  levels: LodLevel[];
  /** Instances now in each level. */
  inLevel: number[];
  /** Per instance, the change in y onto the replaced map models (scene
   *  units), or null. */
  dy: Float32Array | null;
  /** True while the instances stand on the replaced map models (dy applied). */
  lowered: boolean;
  /** Matrices changed since the instanced meshes were last written. */
  dirty: boolean;
}

interface BuiltLayer {
  group: THREE.Group;
  replaces: string[];
  /** Id of the replace layer these instances stand on, or null. */
  standsOn: string | null;
  instances: number;
  triangles: number;
  groups: BuiltGroup[];
}

interface BuiltDressing {
  root: THREE.Group;
  layers: Map<string, BuiltLayer>;
  gltfs: GLTF[];
  materials: THREE.Material[];
}

export interface MapDressingStats {
  instances: number;
  triangles: number;
  /** Instances drawn at each LOD level (index = level), over the shown
   *  layers whose assets have LODs. */
  lodInstances: number[];
}

/** Workspace-relative path of a shared asset, or null when it leaves
 *  `libraries/`. */
function workspaceRel(path: string): string | null {
  const parts = path.replace(/\\/g, '/').split('/').filter((p) => p !== '' && p !== '.');
  if (parts.length < 2 || parts[0] !== 'libraries' || parts.includes('..')) return null;
  return parts.join('/');
}

function tintOf(...tints: (number[] | undefined)[]): THREE.Color | null {
  let r = 1, g = 1, b = 1;
  let any = false;
  for (const t of tints) {
    if (!Array.isArray(t) || t.length < 3) continue;
    r *= Number(t[0]); g *= Number(t[1]); b *= Number(t[2]);
    any = true;
  }
  if (!any || (r === 1 && g === 1 && b === 1)) return null;
  return new THREE.Color(r, g, b);
}

function dirOf(file: string): string {
  const i = file.lastIndexOf('/');
  return i < 0 ? '' : file.slice(0, i + 1);
}

/** Join a document-relative path onto the document's folder ('a/b/../c' -> 'a/c'). */
function joinRel(base: string, rel: string): string {
  const out: string[] = [];
  for (const part of (base + rel).split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') out.pop();
    else out.push(part);
  }
  return out.join('/');
}

export class MapDressingRenderer {
  private readonly parent: THREE.Object3D;
  private readonly built = new Map<string, BuiltDressing>();
  private readonly visible = new Map<string, boolean>();
  private readonly layerOn = new Map<string, Map<string, boolean>>();
  /** Map nodes a replace layer hid, so they come back when it hides. */
  private readonly hiddenByDressing = new Set<THREE.Object3D>();
  private lastCamera: THREE.Vector3 | null = null;
  /** A group's matrices changed (ground switch) and await update(). */
  private dirty = false;

  constructor(parent: THREE.Object3D) {
    this.parent = parent;
  }

  has(file: string): boolean {
    return this.built.has(file);
  }

  /** Build one dressing (hidden until `setVisible`). `fileUrl` maps a path
   *  relative to the map folder to its URL, `workspaceUrl` a path relative to
   *  the workspace root (shared assets). */
  async add(
    file: string,
    doc: MapDressingDoc,
    fileUrl: (rel: string) => string,
    workspaceUrl?: (rel: string) => string,
  ): Promise<void> {
    const base = dirOf(file);
    const url = (rel: string) => fileUrl(joinRel(base, rel));
    const assetUrl = (a: MapDressingAsset): string => {
      if (a.workspace_uri && workspaceUrl) {
        const rel = workspaceRel(a.workspace_uri);
        if (!rel) throw new Error(`asset ${a.id}: workspace_uri must stay under libraries/`);
        return workspaceUrl(rel);
      }
      if (!a.uri) throw new Error(`asset ${a.id}: no uri`);
      return url(a.uri);
    };
    const loader = new GLTFLoader();
    const materials: THREE.Material[] = [];
    // One tinted copy of each material per tint.
    const tinted = new Map<THREE.Material, Map<string, THREE.Material>>();
    const tintMaterial = (m: THREE.Material, tint: THREE.Color | null): THREE.Material => {
      if (!tint) return m;
      const key = tint.getHexString();
      let byKey = tinted.get(m);
      if (!byKey) {
        byKey = new Map();
        tinted.set(m, byKey);
      }
      let c = byKey.get(key);
      if (!c) {
        c = m.clone();
        const col = (c as THREE.MeshStandardMaterial).color;
        if (col) col.multiply(tint);
        byKey.set(key, c);
        materials.push(c);
      }
      return c;
    };
    const assets = new Map((doc.assets ?? []).map((a) => [a.id, a]));
    const layers = doc.layers ?? [];

    const needRecords = layers.some((l) => (l.groups ?? []).length > 0);
    let records: Float32Array | null = null;
    if (needRecords) {
      if (!doc.instances?.file) throw new Error('instance groups without an instances file');
      const resp = await fetch(url(doc.instances.file));
      if (!resp.ok) throw new Error(`${doc.instances.file}: HTTP ${resp.status}`);
      records = new Float32Array(await resp.arrayBuffer());
    }
    // Ground offsets, when a layer stands on a replace layer of this document.
    const replaceIds = new Set(layers.filter((l) => l?.kind === 'replace').map((l) => l.id));
    let groundDy: Float32Array | null = null;
    if (
      records &&
      doc.instances?.ground_dy_file &&
      layers.some((l) => l?.stands_on && replaceIds.has(l.stands_on))
    ) {
      const resp = await fetch(url(doc.instances.ground_dy_file));
      if (!resp.ok) throw new Error(`${doc.instances.ground_dy_file}: HTTP ${resp.status}`);
      groundDy = new Float32Array(await resp.arrayBuffer());
      if (groundDy.length * RECORD_FLOATS !== records.length) {
        throw new Error(`${doc.instances.ground_dy_file}: not one value per record`);
      }
    }

    // Each asset once, whatever the number of groups that use it.
    const gltfs: GLTF[] = [];
    const assetScenes = new Map<string, Promise<THREE.Object3D>>();
    const assetScene = (id: string): Promise<THREE.Object3D> => {
      let p = assetScenes.get(id);
      if (!p) {
        const a = assets.get(id);
        if (!a) return Promise.reject(new Error(`unknown asset ${id}`));
        p = loader.loadAsync(assetUrl(a)).then((g) => {
          gltfs.push(g);
          g.scene.updateMatrixWorld(true);
          return g.scene;
        });
        assetScenes.set(id, p);
      }
      return p;
    };

    const root = new THREE.Group();
    root.name = `Dressing:${file}`;
    root.visible = false;
    const builtLayers = new Map<string, BuiltLayer>();
    const on = new Map<string, boolean>();
    const seen = new Set<string>();
    for (const layer of layers) {
      if (!layer || typeof layer.id !== 'string' || seen.has(layer.id)) continue;
      seen.add(layer.id);
      const group = new THREE.Group();
      group.name = `Dressing_${layer.id}`;
      let instances = 0;
      let triangles = 0;
      const builtGroups: BuiltGroup[] = [];
      const standsOn =
        layer.stands_on && replaceIds.has(layer.stands_on) && groundDy ? layer.stands_on : null;
      if (layer.kind === 'replace') {
        for (const m of layer.models ?? []) {
          const g = await loader.loadAsync(url(m.file));
          gltfs.push(g);
          const holder = new THREE.Group();
          holder.name = m.name ?? m.file;
          if (m.matrix && m.matrix.length === 16) {
            holder.matrix.fromArray(m.matrix);
            holder.matrix.decompose(holder.position, holder.quaternion, holder.scale);
          }
          holder.add(g.scene);
          group.add(holder);
          g.scene.traverse((o) => {
            const mesh = o as THREE.Mesh;
            if (mesh.isMesh) triangles += triangleCount(mesh.geometry);
          });
        }
      } else if (records) {
        for (const grp of layer.groups ?? []) {
          const a = assets.get(grp.asset);
          if (!a || !(grp.count > 0)) continue;
          const scene = await assetScene(grp.asset);
          const first = grp.byte_offset / 4;
          if (first + grp.count * RECORD_FLOATS > records.length) {
            throw new Error(`group ${layer.id}/${grp.asset} runs past the instance file`);
          }
          const unit = a.unit_scale ?? 1;
          const tint = tintOf(a.tint, layer.tint);
          const positions = new Float32Array(grp.count * 3);
          const matrices = new Float32Array(grp.count * 16);
          const inst = new THREE.Matrix4();
          const pos = new THREE.Vector3();
          const quat = new THREE.Quaternion();
          const scl = new THREE.Vector3();
          for (let i = 0; i < grp.count; i++) {
            const r = first + i * RECORD_FLOATS;
            pos.set(records[r], records[r + 1], records[r + 2]);
            quat.set(records[r + 3], records[r + 4], records[r + 5], records[r + 6]);
            const sc = records[r + 7] * unit;
            scl.set(sc, sc, sc);
            inst.compose(pos, quat, scl);
            inst.toArray(matrices, i * 16);
            positions[i * 3] = pos.x;
            positions[i * 3 + 1] = pos.y;
            positions[i * 3 + 2] = pos.z;
          }
          // Levels: the asset's LOD nodes, or the whole asset as one level.
          const levelRoots: { root: THREE.Object3D; max: number }[] = [];
          for (const l of Array.isArray(a.lods) ? a.lods : []) {
            const node = l && typeof l.node === 'string' ? scene.getObjectByName(l.node) : undefined;
            const max = Number(l?.max_distance);
            if (node && Number.isFinite(max)) levelRoots.push({ root: node, max: max * unit });
          }
          if (levelRoots.length === 0) levelRoots.push({ root: scene, max: Infinity });
          const levels: LodLevel[] = [];
          const out = new THREE.Matrix4();
          for (const lr of levelRoots) {
            const level: LodLevel = { meshes: [], maxDist2: lr.max * lr.max, triangles: 0 };
            lr.root.traverse((o) => {
              const mesh = o as THREE.Mesh;
              if (!mesh.isMesh) return;
              const local = mesh.matrixWorld.clone();
              const mat = Array.isArray(mesh.material)
                ? mesh.material.map((m) => tintMaterial(m, tint))
                : tintMaterial(mesh.material, tint);
              const im = new THREE.InstancedMesh(mesh.geometry, mat, grp.count);
              im.name = `${layer.id}:${grp.asset}:${lr.root.name || 'scene'}`;
              im.userData.dressing_asset = grp.asset;
              // Every instance once, so the bounds hold all of them; update()
              // then keeps only the instances in this level.
              for (let i = 0; i < grp.count; i++) {
                inst.fromArray(matrices, i * 16);
                out.multiplyMatrices(inst, local);
                im.setMatrixAt(i, out);
              }
              im.instanceMatrix.needsUpdate = true;
              im.computeBoundingSphere();
              im.computeBoundingBox();
              // Picking thousands of instanced trees per click is slow; the
              // dressing is a look preview, so it stays out of picking.
              im.raycast = () => {};
              group.add(im);
              const tris = triangleCount(mesh.geometry);
              level.meshes.push({ im, local, identity: local.equals(IDENTITY), triangles: tris });
              level.triangles += tris;
            });
            levels.push(level);
          }
          const bg: BuiltGroup = {
            count: grp.count,
            positions,
            matrices,
            levels,
            inLevel: levels.map(() => 0),
            dy:
              standsOn && groundDy
                ? groundDy.slice(first / RECORD_FLOATS, first / RECORD_FLOATS + grp.count)
                : null,
            lowered: false,
            dirty: false,
          };
          bg.inLevel[0] = grp.count;
          // Until the first update every instance draws at the first level.
          for (let li = 1; li < levels.length; li++) for (const m of levels[li].meshes) m.im.count = 0;
          builtGroups.push(bg);
          triangles += levels[0].triangles * grp.count;
          instances += grp.count;
        }
      }
      root.add(group);
      builtLayers.set(layer.id, {
        group,
        replaces: layer.replaces ?? [],
        standsOn,
        instances,
        triangles,
        groups: builtGroups,
      });
      on.set(layer.id, layer.visible !== false);
    }
    this.parent.add(root);
    this.built.set(file, { root, layers: builtLayers, gltfs, materials });
    if (!this.layerOn.has(file)) this.layerOn.set(file, on);
    this.lastCamera = null;
    this.apply();
  }

  /** Put every instance of the LOD groups at the level its distance to the
   *  camera (world position) selects. Cheap when the camera has not moved;
   *  returns true when any instance changed level. */
  update(cameraWorld: THREE.Vector3, force = false): boolean {
    const local = this.parent.worldToLocal(cameraWorld.clone());
    const moved =
      force || !this.lastCamera || this.lastCamera.distanceToSquared(local) >= 1e-4;
    if (!moved && !this.dirty) return false;
    if (moved) this.lastCamera = local;
    const cam = this.lastCamera ?? local;
    this.dirty = false;
    let changed = false;
    const inst = new THREE.Matrix4();
    const out = new THREE.Matrix4();
    for (const d of this.built.values()) {
      for (const l of d.layers.values()) {
        for (const g of l.groups) {
          const lod = !(g.levels.length === 1 && g.levels[0].maxDist2 === Infinity);
          if (!(lod && moved) && !g.dirty) continue;
          if (g.dirty) changed = true;
          g.dirty = false;
          const n = g.levels.length;
          const fill = new Array<number>(n).fill(0);
          for (let i = 0; i < g.count; i++) {
            const dx = g.positions[i * 3] - cam.x;
            const dy = g.positions[i * 3 + 1] - cam.y;
            const dz = g.positions[i * 3 + 2] - cam.z;
            const d2 = dx * dx + dy * dy + dz * dz;
            // The last level also draws past its own range: its end is the
            // source game's view-distance cull, which this preview does not do.
            let li = 0;
            while (li < n - 1 && d2 >= g.levels[li].maxDist2) li++;
            const slot = fill[li]++;
            for (const m of g.levels[li].meshes) {
              const arr = m.im.instanceMatrix.array as Float32Array;
              if (m.identity) {
                arr.set(g.matrices.subarray(i * 16, i * 16 + 16), slot * 16);
              } else {
                inst.fromArray(g.matrices, i * 16);
                out.multiplyMatrices(inst, m.local);
                out.toArray(arr, slot * 16);
              }
            }
          }
          for (let li = 0; li < n; li++) {
            if (fill[li] !== g.inLevel[li]) changed = true;
            g.inLevel[li] = fill[li];
            for (const m of g.levels[li].meshes) {
              m.im.count = fill[li];
              m.im.instanceMatrix.needsUpdate = true;
            }
          }
        }
      }
    }
    return changed;
  }

  setVisible(file: string, visible: boolean): void {
    this.visible.set(file, visible);
    this.apply();
  }

  setLayerVisible(file: string, layerId: string, visible: boolean): void {
    let m = this.layerOn.get(file);
    if (!m) {
      m = new Map();
      this.layerOn.set(file, m);
    }
    m.set(layerId, visible);
    this.apply();
  }

  /** Instances and triangles of the layers now shown (triangles at the
   *  levels the last update chose). */
  stats(): MapDressingStats {
    let instances = 0;
    let triangles = 0;
    const lodInstances: number[] = [];
    for (const [file, d] of this.built) {
      if (!this.visible.get(file)) continue;
      for (const [id, l] of d.layers) {
        if (this.layerOn.get(file)?.get(id) === false) continue;
        instances += l.instances;
        if (l.groups.length === 0) {
          triangles += l.triangles;
          continue;
        }
        for (const g of l.groups) {
          const lod = !(g.levels.length === 1 && g.levels[0].maxDist2 === Infinity);
          g.levels.forEach((lv, li) => {
            triangles += lv.triangles * g.inLevel[li];
            if (lod) lodInstances[li] = (lodInstances[li] ?? 0) + g.inLevel[li];
          });
        }
      }
    }
    return { instances, triangles, lodInstances: Array.from(lodInstances, (v) => v ?? 0) };
  }

  private apply(): void {
    const replaced = new Set<string>();
    for (const [file, d] of this.built) {
      const shown = this.visible.get(file) === true;
      d.root.visible = shown;
      for (const [id, l] of d.layers) {
        const on = this.layerOn.get(file)?.get(id) !== false;
        l.group.visible = on;
        if (shown && on) for (const r of l.replaces) replaced.add(r);
        // Stand on the replaced map models while the layer underneath hides.
        if (l.standsOn) {
          const lower = this.layerOn.get(file)?.get(l.standsOn) === false;
          for (const g of l.groups) if (g.dy && g.lowered !== lower) this.shiftGround(g, lower);
        }
      }
    }
    for (const o of this.hiddenByDressing) o.visible = true;
    this.hiddenByDressing.clear();
    if (replaced.size === 0) return;
    for (const child of this.parent.children) {
      if (child.name.startsWith('Dressing:')) continue;
      if (replaced.has(child.name) && child.visible) {
        child.visible = false;
        this.hiddenByDressing.add(child);
      }
    }
  }

  /** Move a group's instances onto (``lower``) or off the replaced map
   *  models by its ground offsets; update() rewrites the instanced meshes. */
  private shiftGround(g: BuiltGroup, lower: boolean): void {
    if (!g.dy) return;
    const sign = lower ? 1 : -1;
    for (let i = 0; i < g.count; i++) {
      const d = sign * g.dy[i];
      g.positions[i * 3 + 1] += d;
      g.matrices[i * 16 + 13] += d;
    }
    g.lowered = lower;
    g.dirty = true;
    this.dirty = true;
  }

  dispose(): void {
    for (const o of this.hiddenByDressing) o.visible = true;
    this.hiddenByDressing.clear();
    for (const d of this.built.values()) {
      this.parent.remove(d.root);
      d.root.traverse((o) => {
        const im = o as THREE.InstancedMesh;
        if (im.isInstancedMesh) im.dispose();
      });
      for (const g of d.gltfs) disposeGltf(g);
      for (const m of d.materials) m.dispose();
    }
    this.built.clear();
  }
}

function triangleCount(g: THREE.BufferGeometry): number {
  const n = g.index ? g.index.count : (g.getAttribute('position')?.count ?? 0);
  return Math.floor(n / 3);
}

function disposeGltf(g: GLTF): void {
  g.scene.traverse((o) => {
    const mesh = o as THREE.Mesh;
    if (!mesh.isMesh) return;
    mesh.geometry.dispose();
    const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    for (const m of mats) {
      for (const v of Object.values(m)) {
        if (v && (v as THREE.Texture).isTexture) (v as THREE.Texture).dispose();
      }
      m.dispose();
    }
  });
}
