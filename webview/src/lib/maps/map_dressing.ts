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
// inside the map folder.
//
// No Svelte imports: the Maps route owns the lifecycle.

import * as THREE from 'three';
import { GLTFLoader, type GLTF } from 'three/addons/loaders/GLTFLoader.js';

export const MAP_DRESSING_SCHEMA = 'map_dressing/v1';
/** f32 px, py, pz, qx, qy, qz, qw, s */
const RECORD_FLOATS = 8;

export interface MapDressingAsset {
  id: string;
  /** glTF / GLB, relative to the dressing file's folder. */
  uri: string;
  /** Asset units -> scene units (1 for an asset already in scene units). */
  unit_scale?: number;
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
}

export interface MapDressingDoc {
  schema: string;
  title?: string;
  description?: string;
  instances?: { file: string; record?: string };
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

interface BuiltLayer {
  group: THREE.Group;
  replaces: string[];
  instances: number;
  triangles: number;
}

interface BuiltDressing {
  root: THREE.Group;
  layers: Map<string, BuiltLayer>;
  gltfs: GLTF[];
}

export interface MapDressingStats {
  instances: number;
  triangles: number;
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

  constructor(parent: THREE.Object3D) {
    this.parent = parent;
  }

  has(file: string): boolean {
    return this.built.has(file);
  }

  /** Build one dressing (hidden until `setVisible`). `fileUrl` maps a path
   *  relative to the map folder to its URL. */
  async add(file: string, doc: MapDressingDoc, fileUrl: (rel: string) => string): Promise<void> {
    const base = dirOf(file);
    const url = (rel: string) => fileUrl(joinRel(base, rel));
    const loader = new GLTFLoader();
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

    // Each asset once, whatever the number of groups that use it.
    const gltfs: GLTF[] = [];
    const assetScenes = new Map<string, Promise<THREE.Object3D>>();
    const assetScene = (id: string): Promise<THREE.Object3D> => {
      let p = assetScenes.get(id);
      if (!p) {
        const a = assets.get(id);
        if (!a) return Promise.reject(new Error(`unknown asset ${id}`));
        p = loader.loadAsync(url(a.uri)).then((g) => {
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
          const inst = new THREE.Matrix4();
          const pos = new THREE.Vector3();
          const quat = new THREE.Quaternion();
          const scl = new THREE.Vector3();
          const out = new THREE.Matrix4();
          scene.traverse((o) => {
            const mesh = o as THREE.Mesh;
            if (!mesh.isMesh) return;
            const local = mesh.matrixWorld;
            const im = new THREE.InstancedMesh(mesh.geometry, mesh.material, grp.count);
            im.name = `${layer.id}:${grp.asset}`;
            im.userData.dressing_asset = grp.asset;
            for (let i = 0; i < grp.count; i++) {
              const r = first + i * RECORD_FLOATS;
              pos.set(records![r], records![r + 1], records![r + 2]);
              quat.set(records![r + 3], records![r + 4], records![r + 5], records![r + 6]);
              const s = records![r + 7] * unit;
              scl.set(s, s, s);
              inst.compose(pos, quat, scl);
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
            triangles += triangleCount(mesh.geometry) * grp.count;
          });
          instances += grp.count;
        }
      }
      root.add(group);
      builtLayers.set(layer.id, { group, replaces: layer.replaces ?? [], instances, triangles });
      on.set(layer.id, layer.visible !== false);
    }
    this.parent.add(root);
    this.built.set(file, { root, layers: builtLayers, gltfs });
    if (!this.layerOn.has(file)) this.layerOn.set(file, on);
    this.apply();
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

  /** Instances and triangles of the layers now shown. */
  stats(): MapDressingStats {
    let instances = 0;
    let triangles = 0;
    for (const [file, d] of this.built) {
      if (!this.visible.get(file)) continue;
      for (const [id, l] of d.layers) {
        if (this.layerOn.get(file)?.get(id) === false) continue;
        instances += l.instances;
        triangles += l.triangles;
      }
    }
    return { instances, triangles };
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
