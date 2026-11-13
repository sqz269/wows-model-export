// Scene builder for maps that exist only in the export workspace: a map
// folder with an export.json `terrain_heightmap` block and no GLB. It builds
// what a client map's GLB gives the Maps viewer, with the node names the
// viewer's toggles look for:
//
//   Terrain  - the height field as a mesh (u16le samples, bounds, row0)
//   Water    - a flat quad over the bounds at sea level (the FFT ocean
//              replaces it in the default water mode)
//   models   - every glTF that `map_scene.json` places, by its matrix
//
// Frames. The scene frame is the client map GLB's: right-handed, +Y up, in
// the export's units. A client map export stores the height field in its
// native left-handed frame, so scene z = -file z; `map_scene.json`
// `terrain_heightmap_z` gives the sign for a folder that stores it some
// other way (+1: the file frame is the scene frame). The whole map is
// shifted down by its sea level so the ocean surface (y = 0) meets it.
//
// No Svelte imports: the Maps route owns the lifecycle (dispose via
// lib/three/dispose).

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

/** export.json `terrain_heightmap` block (client map exports carry the same). */
export interface TerrainHeightmapBlock {
  file: string;
  encoding?: string;
  width: number;
  height: number;
  bounds: MapBounds;
  min_height: number;
  max_height: number;
  /** Which bound row 0 lies on: 'min_z' (default) or 'max_z'. */
  row0?: string;
  sea_level?: number;
}

export interface MapBounds {
  min_x: number;
  max_x: number;
  min_z: number;
  max_z: number;
}

/** One model `map_scene.json` places. */
export interface MapSceneModel {
  name?: string;
  /** glTF / GLB path, relative to the map folder. */
  file: string;
  /** 4x4, column-major (glTF node matrix): model frame -> scene frame. */
  matrix: number[];
}

/** `map_scene.json`: what a map folder without a GLB needs beside export.json. */
export interface MapSceneDoc {
  schema?: string;
  /** Sign from the height field's file z to scene z. Default -1. */
  terrain_heightmap_z?: number;
  models?: MapSceneModel[];
}

export interface WorkspaceMapScene {
  root: THREE.Group;
  /** Scene-frame XZ extent of the height field (the camera frames it). */
  bounds: MapBounds;
  modelCount: number;
  modelErrors: string[];
  /** Height-field samples skipped per mesh vertex (1 = full resolution). */
  terrainStride: number;
}

/** Height-field mesh resolution cap, in quads per side. */
export const WORKSPACE_TERRAIN_MAX_SEGMENTS = 1024;

interface BuildOptions {
  terrain: TerrainHeightmapBlock;
  scene: MapSceneDoc | null;
  /** URL of a file inside the map folder. */
  fileUrl: (relPath: string) => string;
  /** Metres per scene unit (height colouring thresholds are in metres). */
  metresPerUnit: number;
}

/** Build the scene of a map folder that has a height field and no GLB. */
export async function buildWorkspaceMapScene(opts: BuildOptions): Promise<WorkspaceMapScene> {
  const { terrain, scene } = opts;
  const zSign = scene?.terrain_heightmap_z === 1 ? 1 : -1;
  const seaLevel = terrain.sea_level ?? 0;

  const root = new THREE.Group();
  root.name = 'WorkspaceMap';
  root.position.y = -seaLevel;

  const resp = await fetch(opts.fileUrl(terrain.file));
  if (!resp.ok) throw new Error(`height field ${terrain.file}: HTTP ${resp.status}`);
  const samples = decodeU16le(await resp.arrayBuffer(), terrain.width * terrain.height);
  const { mesh: terrainMesh, stride } = buildTerrainMesh(
    terrain,
    samples,
    zSign,
    opts.metresPerUnit,
  );
  root.add(terrainMesh);

  const bounds = sceneBounds(terrain.bounds, zSign);
  root.add(buildWaterQuad(bounds, seaLevel));

  const models = scene?.models ?? [];
  const loader = new GLTFLoader();
  const modelErrors: string[] = [];
  const loaded = await Promise.allSettled(
    models.map(async (m) => {
      const gltf = await loader.loadAsync(opts.fileUrl(m.file));
      const holder = new THREE.Group();
      holder.name = m.name ?? m.file;
      holder.userData.map_scene_file = m.file;
      holder.matrix.fromArray(m.matrix);
      holder.matrix.decompose(holder.position, holder.quaternion, holder.scale);
      holder.add(gltf.scene);
      return holder;
    }),
  );
  let modelCount = 0;
  for (let i = 0; i < loaded.length; i++) {
    const r = loaded[i];
    if (r.status === 'fulfilled') {
      root.add(r.value);
      modelCount++;
    } else {
      modelErrors.push(`${models[i].file}: ${String(r.reason)}`);
    }
  }
  return { root, bounds, modelCount, modelErrors, terrainStride: stride };
}

function decodeU16le(buf: ArrayBuffer, count: number): Uint16Array {
  if (buf.byteLength < count * 2) {
    throw new Error(`height field holds ${buf.byteLength} bytes, expected ${count * 2}`);
  }
  const view = new DataView(buf);
  const out = new Uint16Array(count);
  for (let i = 0; i < count; i++) out[i] = view.getUint16(i * 2, true);
  return out;
}

function sceneBounds(b: MapBounds, zSign: number): MapBounds {
  return zSign > 0
    ? { min_x: b.min_x, max_x: b.max_x, min_z: b.min_z, max_z: b.max_z }
    : { min_x: b.min_x, max_x: b.max_x, min_z: -b.max_z, max_z: -b.min_z };
}

/** Sample indices along one axis at `stride`, always ending on the last sample. */
function axisIndices(n: number, stride: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < n - 1; i += stride) out.push(i);
  out.push(n - 1);
  return out;
}

function buildTerrainMesh(
  t: TerrainHeightmapBlock,
  samples: Uint16Array,
  zSign: number,
  metresPerUnit: number,
): { mesh: THREE.Mesh; stride: number } {
  const W = t.width;
  const H = t.height;
  const stride = Math.max(1, Math.ceil((Math.max(W, H) - 1) / WORKSPACE_TERRAIN_MAX_SEGMENTS));
  const cols = axisIndices(W, stride);
  const rows = axisIndices(H, stride);
  const nc = cols.length;
  const nr = rows.length;
  const b = t.bounds;
  const hScale = (t.max_height - t.min_height) / 65535;
  const rowFromMax = t.row0 === 'max_z';

  const pos = new Float32Array(nc * nr * 3);
  for (let r = 0; r < nr; r++) {
    const fr = rows[r] / (H - 1);
    const fileZ = rowFromMax
      ? b.max_z - fr * (b.max_z - b.min_z)
      : b.min_z + fr * (b.max_z - b.min_z);
    const z = zSign * fileZ;
    const rowBase = rows[r] * W;
    for (let c = 0; c < nc; c++) {
      const o = (r * nc + c) * 3;
      pos[o] = b.min_x + (cols[c] / (W - 1)) * (b.max_x - b.min_x);
      pos[o + 1] = t.min_height + samples[rowBase + cols[c]] * hScale;
      pos[o + 2] = z;
    }
  }

  // Wind every quad counter-clockwise seen from +Y: which diagonal order
  // that is depends on the direction rows advance in scene z.
  const rowStepZ = zSign * (rowFromMax ? -1 : 1);
  const index = new Uint32Array((nc - 1) * (nr - 1) * 6);
  let k = 0;
  for (let r = 0; r < nr - 1; r++) {
    for (let c = 0; c < nc - 1; c++) {
      const a = r * nc + c;
      const bb = a + 1;
      const d = a + nc;
      const e = d + 1;
      // (a, d, bb) faces +Y when rows advance toward +z, (a, bb, d) otherwise.
      const tris = rowStepZ > 0 ? [a, d, bb, bb, d, e] : [a, bb, d, bb, e, d];
      for (const v of tris) index[k++] = v;
    }
  }

  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geom.setIndex(new THREE.BufferAttribute(index, 1));
  geom.computeVertexNormals();
  geom.setAttribute(
    'color',
    new THREE.BufferAttribute(heightColours(geom, t.sea_level ?? 0, metresPerUnit), 3),
  );
  geom.computeBoundingBox();
  geom.computeBoundingSphere();

  // Same shading model as a client map's GLB terrain (rough, non-metal);
  // the height colours stand in for the lightmap a client export bakes.
  // Pushed back in depth so meshes placed on it win where they coincide.
  const mat = new THREE.MeshStandardMaterial({
    name: 'Terrain',
    vertexColors: true,
    roughness: 1,
    metalness: 0,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  });
  const mesh = new THREE.Mesh(geom, mat);
  mesh.name = 'Terrain';
  return { mesh, stride };
}

// Height palette, linear RGB, thresholds in metres above sea level.
const DEEP = new THREE.Color(0.006, 0.025, 0.066);
const SHALLOW = new THREE.Color(0.03, 0.16, 0.34);
const SAND = new THREE.Color(0.72, 0.62, 0.38);
const GRASS = new THREE.Color(0.09, 0.2, 0.05);
const HIGH = new THREE.Color(0.25, 0.2, 0.14);
const ROCK = new THREE.Color(0.2, 0.18, 0.16);

function heightColours(
  geom: THREE.BufferGeometry,
  seaLevel: number,
  metresPerUnit: number,
): Float32Array {
  const pos = geom.getAttribute('position') as THREE.BufferAttribute;
  const nrm = geom.getAttribute('normal') as THREE.BufferAttribute;
  const out = new Float32Array(pos.count * 3);
  const c = new THREE.Color();
  for (let i = 0; i < pos.count; i++) {
    const m = (pos.getY(i) - seaLevel) * metresPerUnit;
    if (m < 0) {
      c.copy(SHALLOW).lerp(DEEP, Math.min(1, -m / 120));
    } else if (m < 3) {
      c.copy(SAND);
    } else {
      c.copy(GRASS).lerp(HIGH, Math.min(1, (m - 3) / 600));
      const steep = 1 - Math.min(1, Math.max(0, nrm.getY(i)));
      if (steep > 0.3) c.lerp(ROCK, Math.min(1, (steep - 0.3) / 0.3));
    }
    out[i * 3] = c.r;
    out[i * 3 + 1] = c.g;
    out[i * 3 + 2] = c.b;
  }
  return out;
}

/** The flat sea-level quad; material as a client map GLB's `Water`. */
function buildWaterQuad(b: MapBounds, seaLevel: number): THREE.Mesh {
  const geom = new THREE.PlaneGeometry(b.max_x - b.min_x, b.max_z - b.min_z);
  geom.rotateX(-Math.PI / 2);
  const mat = new THREE.MeshStandardMaterial({
    name: 'Water',
    roughness: 1,
    metalness: 0,
    transparent: true,
    opacity: 0.85,
    depthWrite: false,
  });
  mat.color.setRGB(0.1, 0.3, 0.5);
  const mesh = new THREE.Mesh(geom, mat);
  mesh.name = 'Water';
  mesh.position.set((b.min_x + b.max_x) / 2, seaLevel, (b.min_z + b.max_z) / 2);
  return mesh;
}
