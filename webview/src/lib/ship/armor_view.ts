// Armor thickness heat-map. The hull GLB's `Armor` group carries one mesh
// per armor zone (`Armor_Citadel`, `Armor_Bow`, …); every vertex carries a
// `_MATERIAL_ID` (GLTFLoader lowercases custom attributes → `_material_id`)
// that joins to the sidecar's `armor.materials_table[id].thickness_mm`.
//
// We build a per-vertex `color` buffer from thickness → ramp colour and swap
// each armor mesh onto a shared semi-transparent X-ray material with
// `vertexColors` on. The swap is reversible: the original material + any
// pre-existing `color` attribute are stashed per mesh and restored on disable.
//
// X-ray effect: the material is a MeshStandardMaterial patched via
// `onBeforeCompile` so its alpha is driven by a Fresnel term (grazing-angle
// faces opaque, head-on faces transparent) and edges glow in the thickness
// colour. `depthWrite: false` + `DoubleSide` lets every layer blend through,
// so inner belts read behind outer plating. Keeping the base material a
// MeshStandardMaterial (rather than a raw ShaderMaterial) preserves Three's
// colour-management + tone-mapping, so the thickness colours stay accurate.
//
// Lazy-prepared on first enable (cheap — a few thousand verts across ~9
// meshes) and disposed with the ship.

import * as THREE from 'three';
import type { SidecarArmorMaterial } from '$lib/types';

/** X-ray tuning. baseOpacity = floor alpha of head-on faces; fresnelStrength
 *  = extra alpha at grazing angles; fresnelPower shapes the falloff; glow =
 *  how hard edges brighten toward their pure thickness colour. */
export const ARMOR_XRAY = {
  baseOpacity: 0.26,
  fresnelStrength: 0.72,
  fresnelPower: 2.4,
  glow: 0.55,
} as const;

/**
 * Build the shared semi-transparent X-ray armor material. A
 * MeshStandardMaterial (lit, vertex-coloured) whose fragment alpha is
 * replaced by a Fresnel term via `onBeforeCompile`.
 */
export function createArmorXrayMaterial(): THREE.MeshStandardMaterial {
  const material = new THREE.MeshStandardMaterial({
    vertexColors: true,
    metalness: 0,
    roughness: 1,
    side: THREE.DoubleSide,
    transparent: true,
    depthWrite: false,
  });

  material.onBeforeCompile = (shader) => {
    shader.uniforms.uXrayBaseOpacity = { value: ARMOR_XRAY.baseOpacity };
    shader.uniforms.uXrayFresnelStrength = { value: ARMOR_XRAY.fresnelStrength };
    shader.uniforms.uXrayFresnelPower = { value: ARMOR_XRAY.fresnelPower };
    shader.uniforms.uXrayGlow = { value: ARMOR_XRAY.glow };

    shader.fragmentShader = shader.fragmentShader
      .replace(
        '#include <common>',
        `#include <common>
        uniform float uXrayBaseOpacity;
        uniform float uXrayFresnelStrength;
        uniform float uXrayFresnelPower;
        uniform float uXrayGlow;`,
      )
      .replace(
        '#include <opaque_fragment>',
        `{
          // normal (shading normal) + vViewPosition are both in scope here.
          float ndv = clamp(abs(dot(normalize(normal), normalize(vViewPosition))), 0.0, 1.0);
          float xrayFresnel = pow(1.0 - ndv, uXrayFresnelPower);
          float xrayAlpha = clamp(uXrayBaseOpacity + xrayFresnel * uXrayFresnelStrength, 0.0, 1.0);
          // Edge glow toward the pure (unlit) thickness colour so silhouettes pop.
          vec3 xrayOut = outgoingLight + diffuseColor.rgb * (xrayFresnel * uXrayGlow);
          gl_FragColor = vec4(xrayOut, xrayAlpha);
        }`,
      );
  };
  // Distinct cache key so this never shares a compiled program with a plain
  // MeshStandardMaterial (which would skip the onBeforeCompile injection).
  material.customProgramCacheKey = () => 'armor-xray-v1';
  return material;
}

/**
 * Thickness → colour ramp stops (mm, sRGB hex). Matches the side-panel
 * legend. Piecewise-linear between stops; clamped past the ends. 0mm is the
 * "no armor" sentinel (the `_9`/default material) and reads near-black so
 * un-armored shell reads as absence, not "thin".
 */
export const ARMOR_THICKNESS_STOPS: ReadonlyArray<{ mm: number; hex: string }> = [
  { mm: 0, hex: '#2b2f36' },
  { mm: 6, hex: '#3366ff' },
  { mm: 50, hex: '#19d6d6' },
  { mm: 152, hex: '#3fbf3f' },
  { mm: 290, hex: '#e6d619' },
  { mm: 450, hex: '#ff8c1a' },
  { mm: 668, hex: '#e53935' },
  { mm: 889, hex: '#c026d3' },
];

// Pre-parse stops into THREE.Color (working/linear space) once.
const STOP_COLORS = ARMOR_THICKNESS_STOPS.map((s) => new THREE.Color(s.hex));

const _scratch = new THREE.Color();

/** Resolve a thickness (mm) to a working-space colour on the ramp. Writes
 *  into `out` (or a scratch colour) and returns it. */
export function thicknessToColor(mm: number, out: THREE.Color = _scratch): THREE.Color {
  const stops = ARMOR_THICKNESS_STOPS;
  if (mm <= stops[0].mm) return out.copy(STOP_COLORS[0]);
  const last = stops.length - 1;
  if (mm >= stops[last].mm) return out.copy(STOP_COLORS[last]);
  for (let i = 1; i <= last; i++) {
    if (mm <= stops[i].mm) {
      const t = (mm - stops[i - 1].mm) / (stops[i].mm - stops[i - 1].mm);
      return out.copy(STOP_COLORS[i - 1]).lerp(STOP_COLORS[i], t);
    }
  }
  return out.copy(STOP_COLORS[last]);
}

/** CSS hex for a thickness — used by the legend + bottom-panel swatches so
 *  the table colours match the 3D view. */
export function thicknessToColorHex(mm: number): string {
  return `#${thicknessToColor(mm, new THREE.Color()).getHexString()}`;
}

export interface ArmorMeshEntry {
  mesh: THREE.Mesh;
  /** Material(s) the mesh shipped with — restored on disable. */
  originalMaterial: THREE.Material | THREE.Material[];
  /** The mesh's pre-existing `color` attribute (rare on armor), or null. */
  savedColorAttr: THREE.BufferAttribute | THREE.InterleavedBufferAttribute | null;
  /** Computed per-vertex thickness colour buffer. */
  armorColorAttr: THREE.BufferAttribute;
  /** Geometry this entry owns and must dispose (set when cloned per-instance
   *  so shared turret-template geometry isn't mutated). Undefined for hull
   *  armor (unique geometry, released by the hull's disposeTree). */
  ownedGeometry?: THREE.BufferGeometry;
}

/** Resolves a per-vertex `_MATERIAL_ID` to an effective thickness (mm). */
export type ArmorThicknessFn = (matId: number) => number;

export interface ArmorPrep {
  entries: ArmorMeshEntry[];
  /** Shared matte material every armor mesh swaps onto. */
  material: THREE.MeshStandardMaterial;
}

function readMaterialIdAttr(
  geom: THREE.BufferGeometry,
): THREE.BufferAttribute | THREE.InterleavedBufferAttribute | undefined {
  // GLTFLoader lowercases unknown attributes; tolerate either spelling.
  return geom.getAttribute('_material_id') ?? geom.getAttribute('_MATERIAL_ID') ?? undefined;
}

const _localHit = new THREE.Vector3();
const _vA = new THREE.Vector3();
const _vB = new THREE.Vector3();
const _vC = new THREE.Vector3();

/**
 * Read the per-vertex `_MATERIAL_ID` at a raycaster hit on an armor mesh.
 * The id is an integer authored per-vertex (one value per armor plate), so
 * barycentric interpolation across the hit triangle is meaningless — we pick
 * whichever of the triangle's three vertices is nearest the hit point, which
 * resolves correctly even where two plates of differing thickness share an
 * edge. Returns null when the mesh has no material-id attribute or the hit
 * carries no face (non-mesh geometry).
 */
export function materialIdAtIntersection(hit: THREE.Intersection): number | null {
  const mesh = hit.object as THREE.Mesh;
  const geom = mesh.geometry as THREE.BufferGeometry | undefined;
  const face = hit.face;
  if (!geom || !face) return null;
  const matAttr = readMaterialIdAttr(geom);
  if (!matAttr) return null;
  const pos = geom.getAttribute('position');
  if (!pos) return matAttr.getX(face.a);
  // hit.point is world-space; compare against the local-space vertices.
  mesh.worldToLocal(_localHit.copy(hit.point));
  _vA.fromBufferAttribute(pos, face.a);
  _vB.fromBufferAttribute(pos, face.b);
  _vC.fromBufferAttribute(pos, face.c);
  let idx = face.a;
  let best = _localHit.distanceToSquared(_vA);
  const dB = _localHit.distanceToSquared(_vB);
  if (dB < best) {
    best = dB;
    idx = face.b;
  }
  if (_localHit.distanceToSquared(_vC) < best) idx = face.c;
  return matAttr.getX(idx);
}

/**
 * Build reversible thickness-colour swap entries for a set of armor meshes.
 *
 * `thicknessOf` maps each vertex's `_MATERIAL_ID` to an effective thickness;
 * hull armor passes a `materials_table` lookup, per-mount turret armor passes
 * a `mount_armor`-derived one (see `mountArmorThicknessOf`).
 *
 * `cloneGeometry`: clone each mesh's geometry before baking the colour buffer.
 * Required for turret/mount armor — those meshes are clones that SHARE the
 * library template's geometry, so mutating it in place would bleed the colour
 * buffer (and thickness) across every mount of the same asset. Hull armor has
 * unique geometry and skips the clone.
 */
export function buildArmorEntries(
  meshes: Iterable<THREE.Mesh>,
  thicknessOf: ArmorThicknessFn,
  opts: { cloneGeometry?: boolean } = {},
): ArmorMeshEntry[] {
  const entries: ArmorMeshEntry[] = [];
  const col = new THREE.Color();
  let warnedMissing = false;

  for (const mesh of meshes) {
    if (!mesh.isMesh) continue;
    let geom = mesh.geometry as THREE.BufferGeometry;
    const count = geom.getAttribute('position')?.count ?? 0;
    if (!count) continue;

    const matAttr = readMaterialIdAttr(geom);
    if (!matAttr && !warnedMissing) {
      console.warn('[armor] mesh has no _material_id attribute; coloring flat', mesh.name);
      warnedMissing = true;
    }
    const colors = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const id = matAttr ? matAttr.getX(i) : 0;
      thicknessToColor(thicknessOf(id), col);
      colors[i * 3] = col.r;
      colors[i * 3 + 1] = col.g;
      colors[i * 3 + 2] = col.b;
    }

    let ownedGeometry: THREE.BufferGeometry | undefined;
    if (opts.cloneGeometry) {
      geom = geom.clone();
      ownedGeometry = geom;
      mesh.geometry = geom;
    }
    const existing = geom.getAttribute('color') ?? null;
    entries.push({
      mesh,
      originalMaterial: mesh.material,
      savedColorAttr: existing as THREE.BufferAttribute | null,
      armorColorAttr: new THREE.BufferAttribute(colors, 3),
      ownedGeometry,
    });
  }
  return entries;
}

/**
 * Walk the hull `Armor` group and build swap entries + the shared X-ray
 * material. Thickness from the sidecar `materials_table` (summed layers).
 */
export function prepareArmorMeshes(
  armorGroup: THREE.Object3D,
  materialsTable: Record<string, SidecarArmorMaterial>,
): ArmorPrep {
  const meshes: THREE.Mesh[] = [];
  armorGroup.traverse((o) => {
    if ((o as THREE.Mesh).isMesh) meshes.push(o as THREE.Mesh);
  });
  const thicknessOf: ArmorThicknessFn = (id) => materialsTable[String(id)]?.thickness_mm ?? 0;
  return { entries: buildArmorEntries(meshes, thicknessOf), material: createArmorXrayMaterial() };
}

/**
 * Build a thickness function for a turret/secondary mount from the sidecar's
 * `mount_armor[hp]` map. Its keys are `(layer << 16) | material_id`; we mask
 * to the material id and SUM the layers, matching how `materials_table`
 * collapses multi-layer plates into one `thickness_mm`. Falls back to the
 * hull `materials_table` when the mount carries no armor table.
 */
export function mountArmorThicknessOf(
  mountArmor: Record<string, number> | undefined,
  materialsTable: Record<string, SidecarArmorMaterial>,
): ArmorThicknessFn {
  if (mountArmor && Object.keys(mountArmor).length > 0) {
    const byMat = new Map<number, number>();
    for (const [k, mm] of Object.entries(mountArmor)) {
      const mat = Number(k) & 0xffff;
      byMat.set(mat, (byMat.get(mat) ?? 0) + mm);
    }
    return (id) => byMat.get(id) ?? 0;
  }
  return (id) => materialsTable[String(id)]?.thickness_mm ?? 0;
}

/** Swap armor meshes onto (on) / off (off) the thickness material. */
export function applyArmorView(
  entries: ArmorMeshEntry[],
  material: THREE.MeshStandardMaterial,
  on: boolean,
): void {
  for (const e of entries) {
    const geom = e.mesh.geometry as THREE.BufferGeometry;
    if (on) {
      geom.setAttribute('color', e.armorColorAttr);
      e.mesh.material = material;
    } else {
      e.mesh.material = e.originalMaterial;
      if (e.savedColorAttr) geom.setAttribute('color', e.savedColorAttr);
      else geom.deleteAttribute('color');
    }
  }
}

export function disposeArmorView(
  entries: ArmorMeshEntry[],
  material: THREE.MeshStandardMaterial | null,
): void {
  // Hull armor geometry + originalMaterial belong to the hull GLB (released by
  // its disposeTree); we own only per-instance cloned geometry (mount armor)
  // and the shared X-ray material.
  for (const e of entries) e.ownedGeometry?.dispose();
  material?.dispose();
}
