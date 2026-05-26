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
}

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

/**
 * Walk the `Armor` group, build a thickness-colour buffer per mesh, and
 * return the reversible swap entries + the shared material. Meshes without a
 * `_material_id` attribute fall back to a flat 0mm tint (rare; logged once).
 */
export function prepareArmorMeshes(
  armorGroup: THREE.Object3D,
  materialsTable: Record<string, SidecarArmorMaterial>,
): ArmorPrep {
  const material = createArmorXrayMaterial();

  const entries: ArmorMeshEntry[] = [];
  const col = new THREE.Color();
  let warnedMissing = false;

  armorGroup.traverse((obj) => {
    const mesh = obj as THREE.Mesh;
    if (!mesh.isMesh) return;
    const geom = mesh.geometry as THREE.BufferGeometry;
    const count = geom.getAttribute('position')?.count ?? 0;
    if (!count) return;

    const matAttr = readMaterialIdAttr(geom);
    const colors = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const id = matAttr ? matAttr.getX(i) : 0;
      const thickness = materialsTable[String(id)]?.thickness_mm ?? 0;
      thicknessToColor(thickness, col);
      colors[i * 3] = col.r;
      colors[i * 3 + 1] = col.g;
      colors[i * 3 + 2] = col.b;
    }
    if (!matAttr && !warnedMissing) {
      console.warn('[armor] mesh has no _material_id attribute; coloring flat', mesh.name);
      warnedMissing = true;
    }

    const armorColorAttr = new THREE.BufferAttribute(colors, 3);
    const existing = geom.getAttribute('color') ?? null;
    entries.push({
      mesh,
      originalMaterial: mesh.material,
      savedColorAttr: existing as THREE.BufferAttribute | null,
      armorColorAttr,
    });
  });

  return { entries, material };
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
  _entries: ArmorMeshEntry[],
  material: THREE.MeshStandardMaterial,
): void {
  // Per-mesh geometry + originalMaterial belong to the hull GLB and are
  // disposed by the hull's disposeTree; we only own the shared material.
  material.dispose();
}
