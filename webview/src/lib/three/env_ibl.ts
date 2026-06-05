// WG environment IBL: consume the producer's environment library
// (library/environment/manifest.json + the per-space PMREM reflection cubes)
// and turn the chosen space/weather cube into a Three.js PMREM the PBR
// materials can use as `scene.environment` — replacing the procedural
// RoomEnvironment with WG's actual sky radiance.
//
// Pipeline: fetch manifest -> pick space/weather -> fetch the cube DDS ->
// decode 6 faces (mip 0) to linear Float32 RGBA (cube_dds) -> build a float
// CubeTexture -> PMREMGenerator.fromCubemap(). We feed only the sharp mip 0
// and let PMREM do the roughness convolution, so the result matches Three's
// envMap mip-as-roughness sampler (no double-prefilter). The HDR tonemap +
// SH come along for the consumer to data-drive its tonemap pass.
//
// See reference/engine/wg_render_pmrem_ibl.md for the engine-side contract.

import * as THREE from 'three';
import { repoUrl } from '$lib/api/client';
import { decodeCubeDds } from '$lib/dds/cube_dds';

export interface WeatherEnvEntry {
  cube_url: string | null;
  cube: { format: string; width: number; height: number; mips: number; is_cube: boolean } | null;
  cubemaps_path: string | null;
  hdr: Record<string, number | number[] | boolean>;
  sh: number[][] | null;
  pbs_extras: Record<string, number | number[] | boolean>;
}

export interface EnvManifest {
  schema_version: number;
  env_brdf_lut_url: string | null;
  spaces: Record<string, { weather_order: string[]; weathers: Record<string, WeatherEnvEntry> }>;
}

export interface WgEnvironment {
  /** PMREM texture to assign to `scene.environment`. */
  texture: THREE.Texture;
  space: string;
  weather: string;
  /** GT tonemap + bloom + eye-adapt params for the active weather. */
  hdr: Record<string, number | number[] | boolean>;
  /** 9 RGB L2 SH coefficients (diffuse irradiance), if present. */
  sh: number[][] | null;
  /** Log-average luminance of the cube's mip 0 — the scene-average the WG
   *  keyed exposure (`middleGray / avgLum`) divides by. Log-space (geometric
   *  mean) so the small bright sun disc doesn't dominate, matching WG's
   *  log-luminance eye-adaptation. */
  avgLum: number;
  dispose(): void;
}

/** Log-average (geometric-mean) luminance over the 6 cube faces (subsampled). */
function cubeLogAvgLuminance(faces: Float32Array[]): number {
  const EPS = 1e-4;
  const STEP = 4; // sample every 4th texel per axis (×4 channels)
  let logSum = 0;
  let n = 0;
  for (const face of faces) {
    for (let i = 0; i < face.length; i += 4 * STEP) {
      const lum = 0.2126 * face[i] + 0.7152 * face[i + 1] + 0.0722 * face[i + 2];
      logSum += Math.log(Math.max(lum, EPS));
      n++;
    }
  }
  return n > 0 ? Math.exp(logSum / n) : EPS;
}

let _manifest: EnvManifest | null | undefined;

/** Fetch + cache the environment manifest. Returns null when the producer
 *  hasn't built it (the `wows-build-environment-library` step). */
export async function loadEnvManifest(): Promise<EnvManifest | null> {
  if (_manifest !== undefined) return _manifest;
  try {
    const resp = await fetch(repoUrl('library/environment/manifest.json'));
    _manifest = resp.ok ? ((await resp.json()) as EnvManifest) : null;
  } catch {
    _manifest = null;
  }
  return _manifest;
}

/** List `{space, weathers[]}` for a UI selector. */
export async function listEnvironments(): Promise<{ space: string; weathers: string[] }[]> {
  const m = await loadEnvManifest();
  if (!m) return [];
  return Object.entries(m.spaces).map(([space, s]) => ({
    space,
    weathers: s.weather_order?.length ? s.weather_order : Object.keys(s.weathers),
  }));
}

function pickEntry(
  m: EnvManifest,
  space?: string,
  weather?: string,
): { space: string; weather: string; entry: WeatherEnvEntry } | null {
  const spaceKeys = Object.keys(m.spaces);
  if (spaceKeys.length === 0) return null;
  // Prefer the requested space, then a clear-weather default, then the first.
  const sKey =
    (space && m.spaces[space] && space) ||
    (m.spaces['14_Atlantic'] && '14_Atlantic') ||
    spaceKeys[0];
  const s = m.spaces[sKey];
  const wKeys = s.weather_order?.length ? s.weather_order : Object.keys(s.weathers);
  const wKey =
    (weather && s.weathers[weather] && weather) ||
    (wKeys.includes('Default') ? 'Default' : wKeys[0]);
  const entry = s.weathers[wKey];
  if (!entry) return null;
  return { space: sKey, weather: wKey, entry };
}

/**
 * Load the WG environment cube for a space/weather (defaults to a clear-weather
 * representative) and return a PMREM texture ready for `scene.environment`.
 * Returns null if the manifest/cube is unavailable or undecodable — callers
 * should fall back to their procedural environment.
 */
export async function loadWgEnvironment(
  renderer: THREE.WebGLRenderer,
  opts: { space?: string; weather?: string } = {},
): Promise<WgEnvironment | null> {
  const m = await loadEnvManifest();
  if (!m) return null;
  const pick = pickEntry(m, opts.space, opts.weather);
  if (!pick || !pick.entry.cube_url) return null;

  let buf: ArrayBuffer;
  try {
    const resp = await fetch(repoUrl(pick.entry.cube_url));
    if (!resp.ok) return null;
    buf = await resp.arrayBuffer();
  } catch {
    return null;
  }

  const decoded = decodeCubeDds(buf);
  if (!decoded) return null;

  // Measure the cube's log-average luminance BEFORE the faces are disposed —
  // it feeds the consumer's WG keyed exposure.
  const avgLum = cubeLogAvgLuminance(decoded.faces);

  // Get the 6 decoded faces (mip 0) onto a renderable cube. A data-backed
  // float CubeTexture trips Three's cube texSubImage2D upload path, so we go
  // via 6 float DataTextures (a proven upload) blitted into a cube RT.
  const faceTex = decoded.faces.map((data) => {
    const t = new THREE.DataTexture(
      data as unknown as Float32Array<ArrayBuffer>,
      decoded.size,
      decoded.size,
      THREE.RGBAFormat,
      THREE.FloatType,
    );
    t.colorSpace = THREE.NoColorSpace; // linear HDR radiance
    t.minFilter = THREE.LinearFilter;
    t.magFilter = THREE.LinearFilter;
    t.flipY = false;
    t.needsUpdate = true;
    return t;
  });

  const cubeRT = new THREE.WebGLCubeRenderTarget(decoded.size, {
    type: THREE.HalfFloatType,
    colorSpace: THREE.NoColorSpace,
  });
  const blitScene = new THREE.Scene();
  const blitMat = new THREE.MeshBasicMaterial({ depthTest: false, depthWrite: false });
  blitMat.toneMapped = false; // raw linear copy into the cube faces
  const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), blitMat);
  blitScene.add(quad);
  const blitCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);

  const prevRT = renderer.getRenderTarget();
  for (let i = 0; i < 6; i++) {
    blitMat.map = faceTex[i];
    blitMat.needsUpdate = true;
    renderer.setRenderTarget(cubeRT, i);
    renderer.render(blitScene, blitCam);
  }
  renderer.setRenderTarget(prevRT);

  quad.geometry.dispose();
  blitMat.dispose();
  faceTex.forEach((t) => t.dispose());

  const pmrem = new THREE.PMREMGenerator(renderer);
  let rt: THREE.WebGLRenderTarget;
  try {
    rt = pmrem.fromCubemap(cubeRT.texture);
  } catch (e) {
    console.warn('[env-ibl] PMREM.fromCubemap failed:', e);
    cubeRT.dispose();
    pmrem.dispose();
    return null;
  }
  cubeRT.dispose();
  pmrem.dispose();

  return {
    texture: rt.texture,
    space: pick.space,
    weather: pick.weather,
    hdr: pick.entry.hdr ?? {},
    sh: pick.entry.sh ?? null,
    avgLum,
    dispose() {
      rt.dispose();
    },
  };
}
