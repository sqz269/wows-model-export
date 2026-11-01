// Ocean surface mesh + shading.
//
// Geometry: a camera-centred polar grid. Rings are spaced `minSpacing` apart
// out to the radius where the angular spacing reaches it, then grow
// geometrically by (1 + 2π/segments) so cells stay roughly square all the
// way to the far plane. Every vertex samples the three displacement cascades
// with an explicit LOD matched to its own spacing, so waves shorter than the
// grid can resolve are pre-filtered out instead of aliasing (and the far field
// converges to the mip-chain mean, i.e. a flat plane).
//
// Shading: per-pixel normals from the summed slope cascades (with the
// choppy-displacement correction slope/(1+∂D/∂x)), Schlick Fresnel (water F0
// 0.02), sky reflection from the scene's PMREM when a real (WG) environment
// is bound or a procedural daylight sky otherwise, an Atlas-style subsurface
// term (light through wave crests, view-facing glow, sun-facing lift), GGX
// sun glint, Jacobian foam and a distance fade into the scene background.
// FrontSide only — from below the hull stays inspectable.

import * as THREE from 'three';
import type { CascadeOutput } from './simulation';

export interface OceanShadingParams {
  /** Scales vertex displacement and slopes together (0 = flat mirror). */
  displacementScale: number;
  /** Foam appears where the combined turbulence (≈ surface Jacobian, 1 =
   *  undisturbed, < 0 = folded) drops below this. */
  foamCoverage: number;
  foamIntensity: number;
  /** Linear RGB tint of light scattered inside the water. */
  scatterColor: [number, number, number];
  /** Linear RGB of the deep body (ambient-lit). */
  deepColor: [number, number, number];
  /** Minimum GGX roughness of the sun glint (caps its peak brightness). */
  sunRoughness: number;
}

export const DEFAULT_SHADING_PARAMS: OceanShadingParams = {
  displacementScale: 1,
  foamCoverage: 0.64,
  foamIntensity: 1,
  scatterColor: [0.003, 0.016, 0.024],
  deepColor: [0.001, 0.006, 0.012],
  sunRoughness: 0.08,
};

const MIN_SPACING = 0.35;
const SEGMENTS = 256;
// Metres. Large enough to reach the far plane of a map viewer at 1/15 world
// units per metre (~108 km); the coarse outer rings make this ~15 extra rings.
const OUTER_RADIUS = 2.5e6;
const COARSE_RADIUS = 400;

export function buildPolarGrid(
  minSpacing = MIN_SPACING,
  segments = SEGMENTS,
  outerRadius = OUTER_RADIUS,
  coarseRadius = COARSE_RADIUS,
): THREE.BufferGeometry {
  const radii: number[] = [];
  const angular = (2 * Math.PI) / segments;
  let r = 0;
  while (r * angular < minSpacing) {
    r += minSpacing;
    radii.push(r);
  }
  // Square cells (growth 1 + 2π/segments) out to `coarseRadius`; beyond it
  // the displacement is mostly mip-filtered away and, seen from a low
  // camera, hundreds of rings would collapse into a few pixel rows at the
  // horizon (sub-pixel triangles → heavy quad overdraw). There the radial
  // step keeps growing with r, capped at ×1.3 per ring.
  while (r < outerRadius) {
    r *= Math.min(1 + angular * Math.max(1, r / coarseRadius), 1.3);
    radii.push(r);
  }

  const positions = new Float32Array((1 + radii.length * segments) * 3);
  let p = 3; // vertex 0 = centre
  for (const radius of radii) {
    for (let j = 0; j < segments; j++) {
      const a = j * angular;
      positions[p++] = Math.cos(a) * radius;
      positions[p++] = 0;
      positions[p++] = Math.sin(a) * radius;
    }
  }

  const ring = (i: number, j: number) => 1 + i * segments + (j % segments);
  const indices = new Uint32Array((segments + (radii.length - 1) * segments * 2) * 3);
  let q = 0;
  for (let j = 0; j < segments; j++) {
    indices[q++] = 0;
    indices[q++] = ring(0, j + 1);
    indices[q++] = ring(0, j);
  }
  for (let i = 0; i < radii.length - 1; i++) {
    for (let j = 0; j < segments; j++) {
      const a0 = ring(i, j);
      const a1 = ring(i, j + 1);
      const b0 = ring(i + 1, j);
      const b1 = ring(i + 1, j + 1);
      indices[q++] = a0;
      indices[q++] = b1;
      indices[q++] = b0;
      indices[q++] = a0;
      indices[q++] = a1;
      indices[q++] = b1;
    }
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setIndex(new THREE.BufferAttribute(indices, 1));
  geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), outerRadius);
  return geometry;
}

const VERT = /* glsl */ `
uniform sampler2D uDisp0;
uniform sampler2D uDisp1;
uniform sampler2D uDisp2;
uniform vec2 uCenter;
uniform vec2 uCenterUv0;
uniform vec2 uCenterUv1;
uniform vec2 uCenterUv2;
uniform vec3 uLengths;
uniform vec3 uTexel;
uniform float uMinSpacing;
uniform float uAngularStep;
uniform float uCoarseRadius;
uniform float uDispScale;
uniform float uUnitScale;

varying vec3 vWorld;
varying vec2 vOffset;
varying float vViewDepth;

float lodFor(float spacing, float texel) {
  return max(0.0, log2(spacing / texel) + 0.5);
}

void main() {
  vec2 off = position.xz;
  // Filter to the coarser of the two cell edges (the radial step outgrows the
  // angular one past COARSE_RADIUS) so the geometry never aliases.
  float r = length(off);
  float spacing = max(uMinSpacing, min(r * uAngularStep * max(1.0, r / uCoarseRadius), 0.3 * r));
  vec3 d = textureLod(uDisp0, uCenterUv0 + off / uLengths.x, lodFor(spacing, uTexel.x)).xyz
         + textureLod(uDisp1, uCenterUv1 + off / uLengths.y, lodFor(spacing, uTexel.y)).xyz
         + textureLod(uDisp2, uCenterUv2 + off / uLengths.z, lodFor(spacing, uTexel.z)).xyz;
  // The simulation and grid live in metres; uUnitScale maps them into the
  // scene's world units (1 for metre scenes, 1/15 for BW-unit maps).
  vec3 world = (vec3(uCenter.x + off.x, 0.0, uCenter.y + off.y) + d * uDispScale) * uUnitScale;
  vWorld = world;
  vOffset = off;
  vec4 mv = viewMatrix * vec4(world, 1.0);
  vViewDepth = -mv.z;
  gl_Position = projectionMatrix * mv;
}
`;

const FRAG = /* glsl */ `
#include <common>
#ifdef ENVMAP_TYPE_CUBE_UV
uniform sampler2D envMap;
#endif
#include <cube_uv_reflection_fragment>

uniform sampler2D uDisp0;
uniform sampler2D uDisp1;
uniform sampler2D uDisp2;
uniform sampler2D uDeriv0;
uniform sampler2D uDeriv1;
uniform sampler2D uDeriv2;
uniform vec2 uCenter;
uniform vec2 uCenterUv0;
uniform vec2 uCenterUv1;
uniform vec2 uCenterUv2;
uniform vec3 uLengths;
uniform float uDispScale;
uniform vec3 uSunDir;
uniform vec3 uSunColor;
uniform vec3 uSkyColor;
uniform vec3 uBackground;
uniform float uFadeToSky;
uniform float uFogDistance;
uniform vec3 uScatterColor;
uniform vec3 uDeepColor;
uniform float uFoamCoverage;
uniform float uFoamIntensity;
uniform float uSunRoughness;
uniform float uTime;
uniform float uUnitScale;
uniform int uFogMode;
uniform vec3 uFogColor;
uniform float uFogDensity;
uniform float uFogNear;
uniform float uFogFar;

varying vec3 vWorld;
varying vec2 vOffset;
varying float vViewDepth;

vec3 proceduralSky(vec3 dir, vec3 sun) {
  float up = clamp(dir.y, 0.0, 1.0);
  // In a fogged scene (Maps) the fog colour IS the visible horizon, so the
  // reflected horizon takes it (no sea/sky seam) and the zenith takes the
  // hemisphere sky that lights the terrain. Otherwise a fixed daylight sky.
  vec3 horizon = uFogMode != 0 ? uFogColor : vec3(0.62, 0.74, 0.88);
  vec3 zenith = uFogMode != 0 ? uSkyColor : vec3(0.13, 0.33, 0.60);
  vec3 sky = mix(horizon, zenith, pow(up, 0.55));
  float s = clamp(dot(dir, sun), 0.0, 1.0);
  sky += vec3(1.0, 0.85, 0.65) * pow(s, 6.0) * 0.06;
  return sky;
}

vec3 skyRadiance(vec3 dir, float roughness) {
#ifdef ENVMAP_TYPE_CUBE_UV
  return textureCubeUV(envMap, dir, roughness).rgb;
#else
  return proceduralSky(dir, normalize(uSunDir));
#endif
}

float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float valueNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash12(i), hash12(i + vec2(1.0, 0.0)), u.x),
             mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), u.x), u.y);
}

float D_Ggx(float NoH, float a) {
  float a2 = a * a;
  float d = NoH * NoH * (a2 - 1.0) + 1.0;
  return a2 / (PI * d * d);
}

float V_SmithApprox(float NoV, float NoL, float a) {
  float gv = NoL * (NoV * (1.0 - a) + a);
  float gl = NoV * (NoL * (1.0 - a) + a);
  return 0.5 / max(gv + gl, 1e-5);
}

void main() {
  vec2 uv0 = uCenterUv0 + vOffset / uLengths.x;
  vec2 uv1 = uCenterUv1 + vOffset / uLengths.y;
  vec2 uv2 = uCenterUv2 + vOffset / uLengths.z;
  vec4 der = texture(uDeriv0, uv0) + texture(uDeriv1, uv1) + texture(uDeriv2, uv2);
  vec2 slope = uDispScale * der.xy / max(vec2(0.2), 1.0 + uDispScale * der.zw);
  vec3 n = normalize(vec3(-slope.x, 1.0, -slope.y));

  vec3 toCam = cameraPosition - vWorld;
  float dist = length(toCam);
  vec3 V = toCam / max(dist, 1e-9);
  float distM = dist / uUnitScale;
  vec3 L = normalize(uSunDir);
  // Steep, choppy faces can tilt the interpolated normal away from the
  // viewer; bend it back so Fresnel/reflection stay defined.
  float NoVraw = dot(n, V);
  if (NoVraw < 0.02) n = normalize(n + V * (0.02 - NoVraw));

  // Mip-filtering flattens distant normals; hand the lost slope variance to
  // roughness so the horizon glitters/blurs instead of turning into a mirror.
  float roughness = mix(uSunRoughness, 0.35, smoothstep(30.0, 4000.0, distM));
  float NoV = clamp(dot(n, V), 1e-3, 1.0);
  float NoL = clamp(dot(n, L), 0.0, 1.0);
  vec3 H = normalize(L + V);
  float NoH = clamp(dot(n, H), 0.0, 1.0);
  float VoH = clamp(dot(V, H), 0.0, 1.0);

  const float F0 = 0.02;
  float fresnel = F0 + (max(1.0 - roughness, F0) - F0) * pow(1.0 - NoV, 5.0);

  vec3 R = reflect(-V, n);
  R.y = abs(R.y);
  vec3 reflected = skyRadiance(R, roughness);
  vec3 ambient = skyRadiance(vec3(0.0, 1.0, 0.0), 1.0);
#ifndef ENVMAP_TYPE_CUBE_UV
  ambient = uSkyColor;
#endif

  // Atlas-style subsurface scattering (GDC 2019, as popularised by
  // gasgiant / Acerola): light transmitted through crests seen against the
  // sun, a view-facing glow, and a sun-facing lift. Height = displaced y.
  float height = max(0.0, vWorld.y / uUnitScale);
  float k1 = 2.0 * height * pow(clamp(dot(L, -V), 0.0, 1.0), 4.0) * pow(0.5 - 0.5 * dot(L, n), 3.0);
  float k2 = 0.6 * pow(NoV, 2.0);
  float k3 = 0.35 * NoL;
  vec3 body = (k1 + k2 + k3) * uScatterColor * uSunColor + uDeepColor * ambient + uScatterColor * ambient * 0.25;

  float a = roughness * roughness;
  float specF = F0 + (1.0 - F0) * pow(1.0 - VoH, 5.0);
  vec3 spec = uSunColor * min(D_Ggx(NoH, a) * V_SmithApprox(NoV, NoL, a) * specF * NoL, 6.0);

  vec3 color = (1.0 - fresnel) * body + fresnel * reflected + spec;

  // Foam: each cascade only sees its own band's Jacobian, so per-cascade
  // turbulence never folds (min ≈ 0.63 even at 11 m/s). The surface's
  // compression is the summed deficit: J ≈ 1 − Σ(1 − tᶜ) = Σtᶜ − 2. With
  // coverage 0.64 the foamed fraction runs ~0.4% (5.5 m/s) → ~2% (14 m/s),
  // Monahan-like whitecap coverage. A world-anchored noise pattern breaks the
  // mask into streaks.
  float t0 = texture(uDisp0, uv0).w;
  float t1 = texture(uDisp1, uv1).w;
  float t2 = texture(uDisp2, uv2).w;
  float foam = clamp((uFoamCoverage - (t0 + t1 + t2 - 2.0)) * 5.0, 0.0, 1.0);
  vec2 fp = uCenter + vOffset;
  float pattern = 0.55 * valueNoise(fp * 0.9 + uTime * 0.05) + 0.45 * valueNoise(fp * 3.1 - uTime * 0.08);
  foam = clamp(foam * (0.35 + 1.1 * pattern), 0.0, 1.0) * uFoamIntensity;
  foam *= 1.0 - smoothstep(1500.0, 6000.0, distM);
  vec3 foamLit = vec3(0.85) * (ambient * 0.6 + uSunColor * (0.3 + 0.7 * NoL));
  color = mix(color, foamLit, foam);

  // Scene fog when the scene has one (same law as three's Fog/FogExp2, so the
  // sea matches the terrain); otherwise a distance haze into the background
  // that reaches ~1 at the far plane so the far clip never shows a seam.
  // With a sky-cube background that haze takes the sky's horizon radiance.
  float fog;
  vec3 haze;
  if (uFogMode == 1) {
    fog = 1.0 - exp(-uFogDensity * uFogDensity * vViewDepth * vViewDepth);
    haze = uFogColor;
  } else if (uFogMode == 2) {
    fog = smoothstep(uFogNear, uFogFar, vViewDepth);
    haze = uFogColor;
  } else {
    vec3 horizonDir = normalize(vec3(-V.x, 0.0, -V.z) + vec3(0.0, 0.03, 0.0));
    haze = mix(uBackground, skyRadiance(horizonDir, 0.15), uFadeToSky);
    fog = 1.0 - exp(-pow(dist / uFogDistance, 2.0));
  }
  color = mix(color, haze, fog);

  gl_FragColor = vec4(color, 1.0);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}
`;

export class OceanSurface {
  readonly mesh: THREE.Mesh<THREE.BufferGeometry, THREE.ShaderMaterial>;
  readonly material: THREE.ShaderMaterial;
  private readonly uniforms: Record<string, THREE.IUniform>;
  private boundEnv: THREE.Texture | null = null;

  constructor(name = 'OceanSurface') {
    this.uniforms = {
      uDisp0: { value: null },
      uDisp1: { value: null },
      uDisp2: { value: null },
      uDeriv0: { value: null },
      uDeriv1: { value: null },
      uDeriv2: { value: null },
      uCenter: { value: new THREE.Vector2() },
      uCenterUv0: { value: new THREE.Vector2() },
      uCenterUv1: { value: new THREE.Vector2() },
      uCenterUv2: { value: new THREE.Vector2() },
      uLengths: { value: new THREE.Vector3(1, 1, 1) },
      uTexel: { value: new THREE.Vector3(1, 1, 1) },
      uMinSpacing: { value: MIN_SPACING },
      uAngularStep: { value: (2 * Math.PI) / SEGMENTS },
      uCoarseRadius: { value: COARSE_RADIUS },
      uDispScale: { value: DEFAULT_SHADING_PARAMS.displacementScale },
      uSunDir: { value: new THREE.Vector3(0, 1, 0) },
      uSunColor: { value: new THREE.Color(1, 1, 1) },
      uSkyColor: { value: new THREE.Color(0.5, 0.6, 0.8) },
      uBackground: { value: new THREE.Color(0, 0, 0) },
      uFadeToSky: { value: 0 },
      uFogDistance: { value: 2500 },
      uScatterColor: { value: new THREE.Color(...DEFAULT_SHADING_PARAMS.scatterColor) },
      uDeepColor: { value: new THREE.Color(...DEFAULT_SHADING_PARAMS.deepColor) },
      uFoamCoverage: { value: DEFAULT_SHADING_PARAMS.foamCoverage },
      uFoamIntensity: { value: DEFAULT_SHADING_PARAMS.foamIntensity },
      uSunRoughness: { value: DEFAULT_SHADING_PARAMS.sunRoughness },
      uTime: { value: 0 },
      uUnitScale: { value: 1 },
      uFogMode: { value: 0 },
      uFogColor: { value: new THREE.Color() },
      uFogDensity: { value: 0 },
      uFogNear: { value: 0 },
      uFogFar: { value: 1 },
      envMap: { value: null },
    };
    this.material = new THREE.ShaderMaterial({
      uniforms: this.uniforms,
      vertexShader: VERT,
      fragmentShader: FRAG,
      side: THREE.FrontSide,
      transparent: false,
      depthWrite: true,
      depthTest: true,
      // Coplanar wake/splash cards (DEFORM_WATER_SURFACE) sit at y≈0; the
      // bias lets them win the depth test against the mean surface.
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    });
    this.mesh = new THREE.Mesh(buildPolarGrid(), this.material);
    this.mesh.name = name;
    this.mesh.frustumCulled = false;
    this.mesh.matrixAutoUpdate = false;
    this.mesh.renderOrder = -1;
  }

  setShading(p: OceanShadingParams): void {
    const u = this.uniforms;
    u.uDispScale.value = p.displacementScale;
    u.uFoamCoverage.value = p.foamCoverage;
    u.uFoamIntensity.value = p.foamIntensity;
    (u.uScatterColor.value as THREE.Color).setRGB(...p.scatterColor);
    (u.uDeepColor.value as THREE.Color).setRGB(...p.deepColor);
    u.uSunRoughness.value = p.sunRoughness;
  }

  /** Bind a PMREM (CubeUV) environment for reflections, or `null` for the
   *  procedural sky. Recompiles only when the binding changes. */
  setEnvironment(tex: THREE.Texture | null): void {
    const env = tex && tex.mapping === THREE.CubeUVReflectionMapping ? tex : null;
    if (env === this.boundEnv) return;
    this.boundEnv = env;
    // ShaderMaterial has no envMap field; three's program builder still reads
    // `material.envMap` and emits ENVMAP_TYPE_CUBE_UV + CUBEUV_* defines for
    // it, keyed into the program cache, so the PMREM layout is always right.
    (this.material as unknown as { envMap: THREE.Texture | null }).envMap = env;
    this.uniforms.envMap.value = env;
    this.material.needsUpdate = true;
  }

  update(
    camera: THREE.PerspectiveCamera,
    cascades: CascadeOutput[],
    size: number,
    lighting: {
      sunDir: THREE.Vector3;
      sunColor: THREE.Color;
      skyColor: THREE.Color;
      background: THREE.Color;
      /** Background is the bound sky cube, not a flat colour. */
      skyBackground: boolean;
      /** The scene's fog, honoured instead of the built-in haze. */
      fog: THREE.Fog | THREE.FogExp2 | null;
      time: number;
    },
    worldUnitsPerMetre = 1,
  ): void {
    const u = this.uniforms;
    u.uUnitScale.value = worldUnitsPerMetre;
    // Grid centre and texture phase in metres (simulation space).
    const cx = camera.position.x / worldUnitsPerMetre;
    const cz = camera.position.z / worldUnitsPerMetre;
    (u.uCenter.value as THREE.Vector2).set(cx, cz);
    const lengths = u.uLengths.value as THREE.Vector3;
    const texel = u.uTexel.value as THREE.Vector3;
    cascades.forEach((c, i) => {
      u[`uDisp${i}`].value = c.displacement;
      u[`uDeriv${i}`].value = c.derivatives;
      const L = c.length;
      const frac = (v: number) => (((v % L) + L) % L) / L;
      (u[`uCenterUv${i}`].value as THREE.Vector2).set(frac(cx), frac(cz));
      lengths.setComponent(i, L);
      texel.setComponent(i, L / size);
    });
    (u.uSunDir.value as THREE.Vector3).copy(lighting.sunDir);
    (u.uSunColor.value as THREE.Color).copy(lighting.sunColor);
    (u.uSkyColor.value as THREE.Color).copy(lighting.skyColor);
    (u.uBackground.value as THREE.Color).copy(lighting.background);
    u.uFadeToSky.value = lighting.skyBackground && this.boundEnv ? 1 : 0;
    u.uFogDistance.value = camera.far * 0.5;
    const fog = lighting.fog;
    if (fog && (fog as THREE.FogExp2).isFogExp2) {
      u.uFogMode.value = 1;
      u.uFogDensity.value = (fog as THREE.FogExp2).density;
    } else if (fog && (fog as THREE.Fog).isFog) {
      u.uFogMode.value = 2;
      u.uFogNear.value = (fog as THREE.Fog).near;
      u.uFogFar.value = (fog as THREE.Fog).far;
    } else {
      u.uFogMode.value = 0;
    }
    if (fog) (u.uFogColor.value as THREE.Color).copy(fog.color);
    u.uTime.value = lighting.time;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    this.material.dispose();
  }
}
