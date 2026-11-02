// FFT ocean facade: owns the CPU spectrum, the GPU simulation and the surface
// mesh, and exposes one flat parameter set. Built lazily by the scene the
// first time the water is shown (the spectrum build + 12 render targets are
// not free, and most viewer sessions never turn water on).

import * as THREE from 'three';
import {
  buildCascadeSpectra,
  DEFAULT_SPECTRUM_PARAMS,
  significantWaveHeight,
  type OceanSpectrumParams,
  type WaveBand,
} from './spectrum';
import { OceanSimulation } from './simulation';
import { DEFAULT_SHADING_PARAMS, OceanSurface, type OceanShadingParams } from './surface';

export { buildCascadeSpectra, significantWaveHeight, DEFAULT_SPECTRUM_PARAMS };
export type { OceanSpectrumParams, WaveBand, OceanShadingParams };

export interface OceanParams {
  spectrum: OceanSpectrumParams;
  shading: OceanShadingParams;
  /** Horizontal (choppy) displacement multiplier λ. */
  choppiness: number;
  /** Foam relaxation rate (1/s); lower = longer-lived foam trails. */
  foamDecay: number;
  /** FFT intermediate precision. Half is ~2% max error vs a float64 DFT and
   *  half the memory traffic; float only matters for numeric checks. */
  fftPrecision: 'half' | 'float';
  /** Scene world units per metre. The sea is simulated in metres (gravity,
   *  JONSWAP); metre scenes use 1, BW-unit map scenes 1/15 (the 15 m/unit
   *  geometry domain shared with ship models). */
  worldUnitsPerMetre: number;
}

export type OceanParamsPatch = {
  spectrum?: Partial<Omit<OceanSpectrumParams, 'local' | 'largeWave'>> & {
    local?: Partial<WaveBand>;
    largeWave?: Partial<WaveBand>;
  };
  shading?: Partial<OceanShadingParams>;
  choppiness?: number;
  foamDecay?: number;
  fftPrecision?: 'half' | 'float';
  worldUnitsPerMetre?: number;
};

export const DEFAULT_OCEAN_PARAMS: OceanParams = {
  spectrum: DEFAULT_SPECTRUM_PARAMS,
  shading: DEFAULT_SHADING_PARAMS,
  choppiness: 0.9,
  foamDecay: 0.25,
  fftPrecision: 'half',
  worldUnitsPerMetre: 1,
};

function cloneParams(p: OceanParams): OceanParams {
  return {
    spectrum: {
      ...p.spectrum,
      local: { ...p.spectrum.local },
      largeWave: { ...p.spectrum.largeWave },
      cascadeLengths: [...p.spectrum.cascadeLengths] as [number, number, number],
    },
    shading: {
      ...p.shading,
      scatterColor: [...p.shading.scatterColor] as [number, number, number],
      deepColor: [...p.shading.deepColor] as [number, number, number],
    },
    choppiness: p.choppiness,
    foamDecay: p.foamDecay,
    fftPrecision: p.fftPrecision,
    worldUnitsPerMetre: p.worldUnitsPerMetre,
  };
}

export interface OceanLighting {
  sunDir: THREE.Vector3;
  sunColor: THREE.Color;
  skyColor: THREE.Color;
  background: THREE.Color;
  skyBackground: boolean;
  fog: THREE.Fog | THREE.FogExp2 | null;
}

export class Ocean {
  readonly surface: OceanSurface;
  private readonly renderer: THREE.WebGLRenderer;
  private sim: OceanSimulation | null = null;
  private params: OceanParams;
  private spectrumKey = '';
  private spectrumDirty = true;
  private readonly startMs = performance.now();
  private lastTime: number | null = null;
  private frozenTime: number | null = null;

  constructor(renderer: THREE.WebGLRenderer, params: OceanParamsPatch = {}) {
    this.renderer = renderer;
    this.surface = new OceanSurface();
    this.params = cloneParams(DEFAULT_OCEAN_PARAMS);
    this.setParams(params);
  }

  get mesh(): THREE.Mesh {
    return this.surface.mesh;
  }

  getParams(): OceanParams {
    return cloneParams(this.params);
  }

  setParams(patch: OceanParamsPatch): void {
    const p = this.params;
    if (patch.spectrum) {
      const { local, largeWave, ...rest } = patch.spectrum;
      Object.assign(p.spectrum, rest);
      if (local) Object.assign(p.spectrum.local, local);
      if (largeWave) Object.assign(p.spectrum.largeWave, largeWave);
    }
    if (patch.shading) Object.assign(p.shading, patch.shading);
    if (patch.choppiness !== undefined) p.choppiness = patch.choppiness;
    if (patch.foamDecay !== undefined) p.foamDecay = patch.foamDecay;
    if (patch.fftPrecision !== undefined) p.fftPrecision = patch.fftPrecision;
    if (patch.worldUnitsPerMetre !== undefined && patch.worldUnitsPerMetre > 0) {
      p.worldUnitsPerMetre = patch.worldUnitsPerMetre;
    }
    const key = JSON.stringify([p.spectrum, p.fftPrecision]);
    if (key !== this.spectrumKey) {
      this.spectrumKey = key;
      this.spectrumDirty = true;
    }
    this.surface.setShading(p.shading);
  }

  /** Expected significant wave height (m) of the current spectrum. */
  significantWaveHeight(): number {
    return significantWaveHeight(this.params.spectrum);
  }

  /** Freeze the surface at a fixed time (s), or `null` to run on wall clock. */
  setFrozenTime(t: number | null): void {
    this.frozenTime = t;
  }

  /** Simulation time (s) of the last update, wrapped to the loop period. */
  time(): number {
    return this.lastTime ?? 0;
  }

  setEnvironment(tex: THREE.Texture | null): void {
    this.surface.setEnvironment(tex);
  }

  /** This frame's DEFORM_WATER_SURFACE particle layer for the surface shader
   *  (foam coverage, displacement, normals); `null` = none this frame. */
  setDeformLayer(tex: THREE.Texture | null, viewProj?: THREE.Matrix4): void {
    this.surface.setDeformLayer(tex, viewProj);
  }

  /** Live shading parameters (not a copy), for per-frame readers such as the
   *  deform pre-pass that pushes `deformFoamDisplaceMult` into materials. */
  get shading(): Readonly<OceanShadingParams> {
    return this.params.shading;
  }

  private ensureSim(): OceanSimulation {
    if (!this.spectrumDirty && this.sim) return this.sim;
    const spectra = buildCascadeSpectra(this.params.spectrum);
    const n = this.params.spectrum.resolution;
    if (!this.sim || this.sim.size !== n || this.sim.precision !== this.params.fftPrecision) {
      this.sim?.dispose();
      this.sim = new OceanSimulation(this.renderer, spectra, n, { fftPrecision: this.params.fftPrecision });
    } else {
      this.sim.setSpectra(spectra);
    }
    this.spectrumDirty = false;
    return this.sim;
  }

  /** Advance the simulation and refresh the surface uniforms. Call once per
   *  frame, before the scene renders, only while the ocean is visible. */
  update(camera: THREE.PerspectiveCamera, lighting: OceanLighting): void {
    const sim = this.ensureSim();
    const period = Math.max(this.params.spectrum.loopPeriod, 1);
    const raw = this.frozenTime ?? (performance.now() - this.startMs) * 0.001;
    const time = ((raw % period) + period) % period;
    let dt = this.lastTime === null ? 0 : time - this.lastTime;
    if (dt < 0) dt += period;
    this.lastTime = time;
    sim.lambda = this.params.choppiness;
    sim.foamDecay = this.params.foamDecay;
    sim.update(time, dt);
    this.surface.update(camera, sim.outputs(), sim.size, { ...lighting, time }, this.params.worldUnitsPerMetre);
  }

  /** Test hook: the underlying simulation (null until first update). */
  simulation(): OceanSimulation | null {
    return this.sim;
  }

  dispose(): void {
    this.sim?.dispose();
    this.sim = null;
    this.surface.dispose();
  }
}
