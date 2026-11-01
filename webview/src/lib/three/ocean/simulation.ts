// GPU side of the FFT ocean: per frame, per cascade,
//   1. spectrum pass  — h0(k) → h̃(k,t) and its 7 derived spectra, packed as
//                       4 complex fields in 2 MRT outputs,
//   2. inverse FFT    — radix-2 Stockham (autosort, no bit reversal),
//                       log2 N horizontal + log2 N vertical passes,
//   3. combine pass   — scale by choppiness, evaluate the Jacobian, advance
//                       the persistent foam ("turbulence") state, write the
//                       two mipmapped, Repeat-wrapped textures the surface
//                       shader samples.
//
// Packing trick: if A(x) and B(x) are both real, IFFT(Ã + iB̃) = A + iB, so
// each complex channel carries two real fields:
//   fft0 = (Dx + i·Dz, h + i·Dxz)       fft1 = (∂h/∂x + i·∂h/∂z, Dxx + i·Dzz)
// with Dx̃ = i(kx/k)h̃, Sx̃ = i·kx·h̃, Dxx̃ = −(kx²/k)h̃, Dxz̃ = −(kx·kz/k)h̃.
// The i·k̂ sign gives Gerstner-style displacement TOWARD crests (sharp crests,
// Jacobian < 1 on crests → foam where it belongs).
//
// Time convention: h̃ = h0(k)e^{−iωt} + conj(h0(−k))e^{+iωt}, so the energy
// placed at wavevector k by the spectrum travels toward +k (downwind).

import * as THREE from 'three';
import { FullScreenQuad } from 'three/addons/postprocessing/Pass.js';
import type { CascadeSpectrum } from './spectrum';

export interface CascadeOutput {
  /** RGBA: (λ·Dx, h, λ·Dz, turbulence). Repeat-wrapped, mipmapped. */
  displacement: THREE.Texture;
  /** RGBA: (∂h/∂x, ∂h/∂z, λ·∂Dx/∂x, λ·∂Dz/∂z). Repeat-wrapped, mipmapped. */
  derivatives: THREE.Texture;
  length: number;
}

const VERT = /* glsl */ `
out vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4(position.xy, 0.0, 1.0);
}
`;

const COMPLEX = /* glsl */ `
vec2 cmul(vec2 a, vec2 b) { return vec2(a.x * b.x - a.y * b.y, a.x * b.y + a.y * b.x); }
vec2 ci(vec2 a) { return vec2(-a.y, a.x); }
`;

const SPECTRUM_FRAG = /* glsl */ `
precision highp float;
precision highp int;
uniform highp sampler2D uH0;
uniform highp sampler2D uWave;
uniform float uTime;
layout(location = 0) out vec4 o0;
layout(location = 1) out vec4 o1;
${COMPLEX}
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy);
  vec4 h0 = texelFetch(uH0, c, 0);
  vec4 wv = texelFetch(uWave, c, 0);
  float phase = wv.w * uTime;
  vec2 e = vec2(cos(phase), sin(phase));
  vec2 h = cmul(h0.xy, vec2(e.x, -e.y)) + cmul(h0.zw, e);
  vec2 ih = ci(h);
  float kx = wv.x;
  float kz = wv.y;
  float invK = wv.z;
  vec2 dx = ih * (kx * invK);
  vec2 dz = ih * (kz * invK);
  vec2 sx = ih * kx;
  vec2 sz = ih * kz;
  vec2 dxx = -h * (kx * kx * invK);
  vec2 dzz = -h * (kz * kz * invK);
  vec2 dxz = -h * (kx * kz * invK);
  o0 = vec4(dx + ci(dz), h + ci(dxz));
  o1 = vec4(sx + ci(sz), dxx + ci(dzz));
}
`;

// Stockham radix-2 inverse FFT stage (gather form). For stage s, p = 2^s:
//   r = o mod 2p,  i = (o div 2p)·p + (r mod p)
//   out[o] = in[i] + e^{+iπr/p} · in[i + N/2]
// After log2 N stages the result is in natural order. Inverse transform,
// unnormalised (the spectrum amplitudes already carry the normalisation).
const FFT_FRAG = /* glsl */ `
precision highp float;
precision highp int;
uniform highp sampler2D uIn0;
uniform highp sampler2D uIn1;
uniform int uStage;
uniform int uHorizontal;
uniform int uN;
layout(location = 0) out vec4 o0;
layout(location = 1) out vec4 o1;
${COMPLEX}
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy);
  int o = uHorizontal == 1 ? c.x : c.y;
  int p = 1 << uStage;
  int r = o & (2 * p - 1);
  int i = (o >> (uStage + 1)) * p + (r & (p - 1));
  int j = i + (uN >> 1);
  float ang = 3.14159265358979 * float(r) / float(p);
  vec2 w = vec2(cos(ang), sin(ang));
  ivec2 ca = uHorizontal == 1 ? ivec2(i, c.y) : ivec2(c.x, i);
  ivec2 cb = uHorizontal == 1 ? ivec2(j, c.y) : ivec2(c.x, j);
  vec4 a0 = texelFetch(uIn0, ca, 0);
  vec4 b0 = texelFetch(uIn0, cb, 0);
  vec4 a1 = texelFetch(uIn1, ca, 0);
  vec4 b1 = texelFetch(uIn1, cb, 0);
  o0 = vec4(a0.xy + cmul(w, b0.xy), a0.zw + cmul(w, b0.zw));
  o1 = vec4(a1.xy + cmul(w, b1.xy), a1.zw + cmul(w, b1.zw));
}
`;

// Foam state ("turbulence", after gasgiant/FFT-Ocean): snaps down to the
// Jacobian wherever the surface folds, then relaxes back toward 1 at a rate
// that slows while the surface stays compressed — foam lingers behind crests
// instead of strobing with them.
const COMBINE_FRAG = /* glsl */ `
precision highp float;
precision highp int;
uniform highp sampler2D uFft0;
uniform highp sampler2D uFft1;
uniform highp sampler2D uPrev;
uniform float uLambda;
uniform float uDt;
uniform float uFoamDecay;
uniform int uReset;
layout(location = 0) out vec4 o0;
layout(location = 1) out vec4 o1;
void main() {
  ivec2 c = ivec2(gl_FragCoord.xy);
  vec4 f0 = texelFetch(uFft0, c, 0);
  vec4 f1 = texelFetch(uFft1, c, 0);
  float dxx = uLambda * f1.z;
  float dzz = uLambda * f1.w;
  float dxz = uLambda * f0.w;
  float jacobian = (1.0 + dxx) * (1.0 + dzz) - dxz * dxz;
  float prev = uReset == 1 ? 1.0 : texelFetch(uPrev, c, 0).w;
  float turbulence = min(jacobian, prev + uDt * uFoamDecay / max(jacobian, 0.5));
  o0 = vec4(uLambda * f0.x, f0.z, uLambda * f0.y, turbulence);
  o1 = vec4(f1.x, f1.y, dxx, dzz);
}
`;

const COPY_FRAG = /* glsl */ `
precision highp float;
uniform highp sampler2D uSrc;
layout(location = 0) out vec4 o0;
void main() { o0 = texelFetch(uSrc, ivec2(gl_FragCoord.xy), 0); }
`;

function shader(fragmentShader: string, uniforms: Record<string, THREE.IUniform>): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    glslVersion: THREE.GLSL3,
    vertexShader: VERT,
    fragmentShader,
    uniforms,
    depthTest: false,
    depthWrite: false,
    blending: THREE.NoBlending,
  });
}

function dataTexture(data: Float32Array, n: number): THREE.DataTexture {
  const tex = new THREE.DataTexture(data as BufferSource, n, n, THREE.RGBAFormat, THREE.FloatType);
  tex.minFilter = THREE.NearestFilter;
  tex.magFilter = THREE.NearestFilter;
  tex.generateMipmaps = false;
  tex.needsUpdate = true;
  return tex;
}

interface CascadeState {
  length: number;
  h0: THREE.DataTexture;
  wave: THREE.DataTexture;
  h0Data: Float32Array;
  waveData: Float32Array;
  /** Output ping-pong: read [cur], write [1 − cur]. */
  out: [THREE.WebGLRenderTarget, THREE.WebGLRenderTarget];
  cur: 0 | 1;
}

export class OceanSimulation {
  readonly size: number;
  readonly fftType: THREE.TextureDataType;
  /** Requested precision (fftType may still be half if float isn't renderable). */
  readonly precision: 'float' | 'half';
  private readonly renderer: THREE.WebGLRenderer;
  private readonly quad = new FullScreenQuad();
  private readonly ping: [THREE.WebGLRenderTarget, THREE.WebGLRenderTarget];
  private readonly cascades: CascadeState[] = [];
  private readonly spectrumMat: THREE.ShaderMaterial;
  private readonly fftMat: THREE.ShaderMaterial;
  private readonly combineMat: THREE.ShaderMaterial;
  private copyMat: THREE.ShaderMaterial | null = null;
  private copyTarget: THREE.WebGLRenderTarget | null = null;
  private reset = true;
  lambda = 1;
  foamDecay = 0.5;

  constructor(
    renderer: THREE.WebGLRenderer,
    spectra: CascadeSpectrum[],
    size: number,
    opts: { fftPrecision?: 'float' | 'half' } = {},
  ) {
    this.renderer = renderer;
    this.size = size;
    // The FFT is bandwidth-bound (2 MRT outputs + 4 fetches per texel per
    // pass, 2·log2 N passes per cascade); half-float intermediates halve the
    // traffic. Float32 only when asked for and renderable.
    this.precision = opts.fftPrecision ?? 'half';
    const wantFloat = this.precision === 'float';
    this.fftType =
      wantFloat && renderer.extensions.has('EXT_color_buffer_float') ? THREE.FloatType : THREE.HalfFloatType;
    const fftTarget = () =>
      new THREE.WebGLRenderTarget(size, size, {
        count: 2,
        type: this.fftType,
        format: THREE.RGBAFormat,
        minFilter: THREE.NearestFilter,
        magFilter: THREE.NearestFilter,
        generateMipmaps: false,
        depthBuffer: false,
        stencilBuffer: false,
      });
    this.ping = [fftTarget(), fftTarget()];
    this.ping[0].texture.name = 'OceanFftA';
    this.ping[1].texture.name = 'OceanFftB';

    this.spectrumMat = shader(SPECTRUM_FRAG, {
      uH0: { value: null },
      uWave: { value: null },
      uTime: { value: 0 },
    });
    this.fftMat = shader(FFT_FRAG, {
      uIn0: { value: null },
      uIn1: { value: null },
      uStage: { value: 0 },
      uHorizontal: { value: 1 },
      uN: { value: size },
    });
    this.combineMat = shader(COMBINE_FRAG, {
      uFft0: { value: null },
      uFft1: { value: null },
      uPrev: { value: null },
      uLambda: { value: 1 },
      uDt: { value: 0 },
      uFoamDecay: { value: 0.5 },
      uReset: { value: 1 },
    });

    // Fetch-bound on integrated GPUs: 8× anisotropy on all six RGBA16F
    // samplers cost ~9 ms/frame at 1053×504 on a Radeon iGPU. Normals keep
    // 4× (grazing-angle detail is the look); displacement .w only feeds the
    // fragment foam term, so it stays isotropic.
    const normalAnisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
    for (const s of spectra) {
      const outTarget = () => {
        const rt = new THREE.WebGLRenderTarget(size, size, {
          count: 2,
          type: THREE.HalfFloatType,
          format: THREE.RGBAFormat,
          minFilter: THREE.LinearMipmapLinearFilter,
          magFilter: THREE.LinearFilter,
          wrapS: THREE.RepeatWrapping,
          wrapT: THREE.RepeatWrapping,
          generateMipmaps: true,
          depthBuffer: false,
          stencilBuffer: false,
        });
        rt.textures[0].name = 'OceanDisplacement';
        rt.textures[1].name = 'OceanDerivatives';
        rt.textures[1].anisotropy = normalAnisotropy;
        return rt;
      };
      this.cascades.push({
        length: s.length,
        h0Data: s.h0.slice(),
        waveData: s.wave.slice(),
        h0: null as unknown as THREE.DataTexture,
        wave: null as unknown as THREE.DataTexture,
        out: [outTarget(), outTarget()],
        cur: 0,
      });
      const c = this.cascades[this.cascades.length - 1];
      c.h0 = dataTexture(c.h0Data, size);
      c.wave = dataTexture(c.waveData, size);
    }
  }

  /** Swap in a rebuilt spectrum (same resolution + cascade count). */
  setSpectra(spectra: CascadeSpectrum[]): void {
    spectra.forEach((s, i) => {
      const c = this.cascades[i];
      if (!c) return;
      c.length = s.length;
      c.h0Data.set(s.h0);
      c.waveData.set(s.wave);
      c.h0.needsUpdate = true;
      c.wave.needsUpdate = true;
    });
    this.reset = true;
  }

  /** Advance to absolute time `time` (s, already wrapped to the loop period). */
  update(time: number, dt: number): void {
    const renderer = this.renderer;
    const prevTarget = renderer.getRenderTarget();
    const prevAutoClear = renderer.autoClear;
    const prevXr = renderer.xr.enabled;
    renderer.autoClear = false;
    renderer.xr.enabled = false;
    try {
      const log2n = Math.round(Math.log2(this.size));
      for (const c of this.cascades) {
        this.spectrumMat.uniforms.uH0.value = c.h0;
        this.spectrumMat.uniforms.uWave.value = c.wave;
        this.spectrumMat.uniforms.uTime.value = time;
        this.draw(this.spectrumMat, this.ping[0]);

        let src = 0;
        for (const horizontal of [1, 0]) {
          for (let stage = 0; stage < log2n; stage++) {
            const u = this.fftMat.uniforms;
            u.uIn0.value = this.ping[src].textures[0];
            u.uIn1.value = this.ping[src].textures[1];
            u.uStage.value = stage;
            u.uHorizontal.value = horizontal;
            this.draw(this.fftMat, this.ping[1 - src]);
            src = 1 - src;
          }
        }

        const next = (1 - c.cur) as 0 | 1;
        const u = this.combineMat.uniforms;
        u.uFft0.value = this.ping[src].textures[0];
        u.uFft1.value = this.ping[src].textures[1];
        u.uPrev.value = c.out[c.cur].textures[0];
        u.uLambda.value = this.lambda;
        u.uDt.value = Math.min(Math.max(dt, 0), 0.25);
        u.uFoamDecay.value = this.foamDecay;
        u.uReset.value = this.reset ? 1 : 0;
        this.draw(this.combineMat, c.out[next]);
        c.cur = next;
      }
      this.reset = false;
    } finally {
      renderer.setRenderTarget(prevTarget);
      renderer.autoClear = prevAutoClear;
      renderer.xr.enabled = prevXr;
    }
  }

  outputs(): CascadeOutput[] {
    return this.cascades.map((c) => ({
      displacement: c.out[c.cur].textures[0],
      derivatives: c.out[c.cur].textures[1],
      length: c.length,
    }));
  }

  /** Debug/test: read back a cascade's current output (or the raw IFFT
   *  result of the last cascade processed, `which = 'fft0' | 'fft1'`) as
   *  float RGBA, row-major, N×N. */
  readback(cascade: number, which: 'displacement' | 'derivatives' | 'fft0' | 'fft1'): Float32Array {
    const n = this.size;
    if (!this.copyMat) {
      this.copyMat = shader(COPY_FRAG, { uSrc: { value: null } });
      this.copyTarget = new THREE.WebGLRenderTarget(n, n, {
        type: THREE.FloatType,
        minFilter: THREE.NearestFilter,
        magFilter: THREE.NearestFilter,
        generateMipmaps: false,
        depthBuffer: false,
        stencilBuffer: false,
      });
    }
    const c = this.cascades[cascade];
    const src =
      which === 'displacement'
        ? c.out[c.cur].textures[0]
        : which === 'derivatives'
          ? c.out[c.cur].textures[1]
          : this.ping[0].textures[which === 'fft0' ? 0 : 1];
    this.copyMat.uniforms.uSrc.value = src;
    const prevTarget = this.renderer.getRenderTarget();
    const out = new Float32Array(n * n * 4);
    try {
      this.draw(this.copyMat, this.copyTarget!);
      this.renderer.readRenderTargetPixels(this.copyTarget!, 0, 0, n, n, out);
    } finally {
      this.renderer.setRenderTarget(prevTarget);
    }
    return out;
  }

  private draw(material: THREE.ShaderMaterial, target: THREE.WebGLRenderTarget): void {
    this.quad.material = material;
    this.renderer.setRenderTarget(target);
    this.quad.render(this.renderer);
  }

  dispose(): void {
    this.quad.dispose();
    for (const rt of this.ping) rt.dispose();
    for (const c of this.cascades) {
      c.h0.dispose();
      c.wave.dispose();
      c.out[0].dispose();
      c.out[1].dispose();
    }
    this.spectrumMat.dispose();
    this.fftMat.dispose();
    this.combineMat.dispose();
    this.copyMat?.dispose();
    this.copyTarget?.dispose();
  }
}
