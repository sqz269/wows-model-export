// Initial ocean spectrum h0(k) for the FFT ocean (CPU side, rebuilt only on
// parameter change).
//
// Model: two-band fetch-limited JONSWAP with TMA depth attenuation and the
// Horvath (2015, "Empirical directional wave spectra for computer graphics")
// directional spreading — a broad cos² lobe blended with a Donelan-Banner
// style cos^2s(θ/2), whose sharpness rises with `swell`. Formulation follows
// gasgiant/FFT-Ocean (MIT), the same reference poseidon adapts.
//
// The parameter vocabulary deliberately mirrors the native WoWS sea uniforms
// (reference/engine/wg_render_ocean_fft.md §4): g_windSpeed / g_windAngle /
// g_fetch / g_spectrumAmplitude / g_peakEnhance / g_spreadBlend / g_swell,
// each with a `LargeWave` twin gated by g_useLargeWave, plus g_fftChoppyScale.
// WG's actual per-map values are not extracted yet; the defaults here are
// visual choices, not engine constants.
//
// Normalisation: with S2D(k) the 2D wavenumber spectrum (∫ S2D d²k = height
// variance) and h̃(k,t) = h0(k)e^{iωt} + conj(h0(−k))e^{−iωt}, choosing
// E|h0|² = S2D·Δk²/2 makes Σ_k E|h̃(k)|² equal the variance exactly.

export const GRAVITY = 9.81;

export interface WaveBand {
  /** Wind speed at 10 m (m/s). */
  windSpeed: number;
  /** Direction the waves travel toward, degrees from +X toward +Z. */
  windDirectionDeg: number;
  /** Fetch length (km). Longer fetch = longer, more developed waves. */
  fetchKm: number;
  /** JONSWAP peak enhancement γ (3.3 is the canonical mean). */
  peakEnhancement: number;
  /** 0 = broad cos² spreading, 1 = narrow cos^2s spreading. */
  spreadBlend: number;
  /** 0..1 — narrows the spreading further (long-crested swell). */
  swell: number;
  /** Energy multiplier (0 disables the band). */
  scale: number;
  /** Damps very short waves: S *= exp(−(k·fade)²), metres. */
  shortWavesFade: number;
}

export interface OceanSpectrumParams {
  local: WaveBand;
  /** Second band (WG `*LargeWave`): long swell from a distant storm. */
  largeWave: WaveBand;
  useLargeWave: boolean;
  /** Water depth (m) for TMA attenuation + finite-depth dispersion. */
  depth: number;
  /** FFT resolution per cascade (native: 256 / 128 / 64 by quality). */
  resolution: 64 | 128 | 256;
  /** Tile size of each of the three cascades, metres (large → small). */
  cascadeLengths: [number, number, number];
  /** Every ω is rounded to a multiple of 2π/loopPeriod so the whole field
   *  repeats exactly after this many seconds; the simulation wraps time to
   *  it, which keeps the shader's ω·t phase inside float32 precision. */
  loopPeriod: number;
  seed: number;
}

export const DEFAULT_SPECTRUM_PARAMS: OceanSpectrumParams = {
  local: {
    windSpeed: 5.5,
    windDirectionDeg: 25,
    fetchKm: 80,
    peakEnhancement: 3.3,
    spreadBlend: 0.85,
    swell: 0.15,
    scale: 1,
    shortWavesFade: 0.02,
  },
  largeWave: {
    windSpeed: 5,
    windDirectionDeg: -20,
    fetchKm: 600,
    peakEnhancement: 3.3,
    spreadBlend: 1,
    swell: 1,
    scale: 0.12,
    shortWavesFade: 0.5,
  },
  useLargeWave: true,
  depth: 500,
  resolution: 256,
  cascadeLengths: [400, 41, 7],
  loopPeriod: 1000,
  seed: 20260608,
};

export interface CascadeSpectrum {
  /** Tile size (m). */
  length: number;
  /** RGBA32F N×N: (h0(k).re, h0(k).im, conj(h0(−k)).re, conj(h0(−k)).im). */
  h0: Float32Array;
  /** RGBA32F N×N: (kx, kz, 1/|k| (0 at k=0), ω(k)). */
  wave: Float32Array;
  /** Wavenumber band [kLow, kHigh) this cascade owns. */
  kLow: number;
  kHigh: number;
}

/** Lanczos log-gamma (g = 7, n = 9), accurate to ~1e-13 for x > 0. */
function lgamma(x: number): number {
  const c = [
    0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313,
    -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6,
    1.5056327351493116e-7,
  ];
  if (x < 0.5) return Math.log(Math.PI / Math.abs(Math.sin(Math.PI * x))) - lgamma(1 - x);
  x -= 1;
  let a = c[0];
  const t = x + 7.5;
  for (let i = 1; i < 9; i++) a += c[i] / (x + i);
  return 0.5 * Math.log(2 * Math.PI) + (x + 0.5) * Math.log(t) - t + Math.log(a);
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussianPair(rand: () => number): [number, number] {
  const u1 = Math.max(rand(), 1e-12);
  const u2 = rand();
  const r = Math.sqrt(-2 * Math.log(u1));
  return [r * Math.cos(2 * Math.PI * u2), r * Math.sin(2 * Math.PI * u2)];
}

/** Finite-depth dispersion ω(k) = sqrt(g k tanh(k h)). */
export function dispersion(k: number, depth: number): number {
  return Math.sqrt(GRAVITY * k * Math.tanh(Math.min(k * depth, 20)));
}

function dispersionDerivative(k: number, depth: number): number {
  const kh = Math.min(k * depth, 20);
  const th = Math.tanh(kh);
  const ch = Math.cosh(kh);
  const w = dispersion(k, depth);
  return w > 0 ? (GRAVITY * (th + kh / (ch * ch))) / (2 * w) : 0;
}

interface BandConstants {
  alpha: number;
  omegaP: number;
  gamma: number;
  angle: number;
  spreadBlend: number;
  swell: number;
  scale: number;
  fade: number;
}

function bandConstants(b: WaveBand): BandConstants {
  const U = Math.max(b.windSpeed, 0.1);
  const F = Math.max(b.fetchKm, 0.01) * 1000;
  return {
    alpha: 0.076 * Math.pow((U * U) / (F * GRAVITY), 0.22),
    omegaP: 22 * Math.pow((GRAVITY * GRAVITY) / (U * F), 1 / 3),
    gamma: Math.max(b.peakEnhancement, 1),
    angle: (b.windDirectionDeg * Math.PI) / 180,
    spreadBlend: Math.min(Math.max(b.spreadBlend, 0), 1),
    swell: Math.min(Math.max(b.swell, 0), 1),
    scale: Math.max(b.scale, 0),
    fade: Math.max(b.shortWavesFade, 0),
  };
}

function tma(omega: number, depth: number): number {
  const wh = omega * Math.sqrt(depth / GRAVITY);
  if (wh <= 1) return 0.5 * wh * wh;
  if (wh < 2) return 1 - 0.5 * (2 - wh) * (2 - wh);
  return 1;
}

/** JONSWAP S(ω) (m²·s), one-sided over ω > 0. */
function jonswap(omega: number, c: BandConstants, depth: number): number {
  const sigma = omega <= c.omegaP ? 0.07 : 0.09;
  const d = omega - c.omegaP;
  const r = Math.exp(-(d * d) / (2 * sigma * sigma * c.omegaP * c.omegaP));
  const w4 = Math.pow(c.omegaP / omega, 4);
  return (
    c.scale *
    tma(omega, depth) *
    ((c.alpha * GRAVITY * GRAVITY) / Math.pow(omega, 5)) *
    Math.exp(-1.25 * w4) *
    Math.pow(c.gamma, r)
  );
}

function wrapAngle(a: number): number {
  a = (a + Math.PI) % (2 * Math.PI);
  if (a < 0) a += 2 * Math.PI;
  return a - Math.PI;
}

/** Directional distribution D(θ, ω), normalised so ∫_{−π}^{π} D dθ = 1. */
function directional(theta: number, omega: number, c: BandConstants): number {
  const t = wrapAngle(theta - c.angle);
  const ratio = omega / c.omegaP;
  let s = ratio > 1 ? 9.77 * Math.pow(ratio, -2.5) : 6.97 * Math.pow(ratio, 5);
  s += 16 * Math.tanh(Math.min(ratio, 20)) * c.swell * c.swell;
  s = Math.max(s, 1e-3);
  const q = Math.exp(lgamma(s + 1) - lgamma(s + 0.5)) / (2 * Math.sqrt(Math.PI));
  const narrow = q * Math.pow(Math.abs(Math.cos(0.5 * t)), 2 * s);
  const broad = Math.abs(t) < Math.PI / 2 ? (2 / Math.PI) * Math.cos(t) * Math.cos(t) : 0;
  return broad + (narrow - broad) * c.spreadBlend;
}

/** Wavenumber boundaries between the three cascades. Each cascade owns a
 *  disjoint band so no wave is counted twice; the boundary sits a few of the
 *  next cascade's fundamental wavenumbers in, clamped under this cascade's
 *  Nyquist so low resolutions don't leave a gap. */
export function cascadeBands(lengths: [number, number, number], N: number): [number, number][] {
  const nyquist = (L: number) => (Math.PI * N) / L;
  const b1 = Math.min((6 * 2 * Math.PI) / lengths[1], 0.8 * nyquist(lengths[0]));
  const b2 = Math.min((6 * 2 * Math.PI) / lengths[2], 0.8 * nyquist(lengths[1]));
  return [
    [0, b1],
    [b1, b2],
    [b2, Infinity],
  ];
}

export function buildCascadeSpectra(p: OceanSpectrumParams): CascadeSpectrum[] {
  const N = p.resolution;
  const bands = cascadeBands(p.cascadeLengths, N);
  const local = bandConstants(p.local);
  const large = p.useLargeWave ? bandConstants(p.largeWave) : null;
  const depth = Math.max(p.depth, 1);
  const omega0 = (2 * Math.PI) / Math.max(p.loopPeriod, 1);

  return p.cascadeLengths.map((L, c) => {
    const [kLow, kHigh] = bands[c];
    const dk = (2 * Math.PI) / L;
    const rand = mulberry32((p.seed + c * 7919) >>> 0);
    const h0re = new Float32Array(N * N);
    const h0im = new Float32Array(N * N);
    const wave = new Float32Array(N * N * 4);
    const half = N / 2;

    for (let m = 0; m < N; m++) {
      const mm = m < half ? m : m - N;
      for (let n = 0; n < N; n++) {
        const nn = n < half ? n : n - N;
        const i = m * N + n;
        // Draw the Gaussians unconditionally so the field for a given seed
        // doesn't reshuffle when band limits or wind change.
        const [g1, g2] = gaussianPair(rand);
        const kx = nn * dk;
        const kz = mm * dk;
        const k = Math.hypot(kx, kz);
        const omega = dispersion(k, depth);
        wave[i * 4] = kx;
        wave[i * 4 + 1] = kz;
        wave[i * 4 + 2] = k > 0 ? 1 / k : 0;
        wave[i * 4 + 3] = Math.round(omega / omega0) * omega0;
        // Nyquist row/column have no Hermitian partner: leave them empty.
        if (n === half || m === half || k <= 0 || k < kLow || k >= kHigh) continue;
        const theta = Math.atan2(kz, kx);
        const dwdk = dispersionDerivative(k, depth);
        let s =
          jonswap(omega, local, depth) * directional(theta, omega, local) * Math.exp(-((k * local.fade) ** 2));
        if (large) {
          s += jonswap(omega, large, depth) * directional(theta, omega, large) * Math.exp(-((k * large.fade) ** 2));
        }
        const s2d = (s * dwdk) / k;
        if (!(s2d > 0) || !Number.isFinite(s2d)) continue;
        const amp = 0.5 * Math.sqrt(s2d * dk * dk);
        h0re[i] = g1 * amp;
        h0im[i] = g2 * amp;
      }
    }

    const h0 = new Float32Array(N * N * 4);
    for (let m = 0; m < N; m++) {
      const mNeg = (N - m) % N;
      for (let n = 0; n < N; n++) {
        const i = m * N + n;
        const j = mNeg * N + ((N - n) % N);
        h0[i * 4] = h0re[i];
        h0[i * 4 + 1] = h0im[i];
        h0[i * 4 + 2] = h0re[j];
        h0[i * 4 + 3] = -h0im[j];
      }
    }
    return { length: L, h0, wave, kLow, kHigh };
  });
}

/** Expected significant wave height Hs = 4·sqrt(variance) of the configured
 *  spectrum (diagnostic; integrates the 1D JONSWAP bands). */
export function significantWaveHeight(p: OceanSpectrumParams): number {
  const depth = Math.max(p.depth, 1);
  const bands = [bandConstants(p.local)];
  if (p.useLargeWave) bands.push(bandConstants(p.largeWave));
  let variance = 0;
  const steps = 4000;
  const wMax = 12;
  for (const b of bands) {
    for (let i = 1; i <= steps; i++) {
      const w = (i / steps) * wMax;
      variance += jonswap(w, b, depth) * (wMax / steps);
    }
  }
  return 4 * Math.sqrt(variance);
}
