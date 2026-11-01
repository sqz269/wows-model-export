// In the running Vite browser:
// (await import('/scripts/test-ocean-fft.browser.mjs')).verifyOceanFft()
//
// Checks the GPU Stockham IFFT + spectrum packing against a direct CPU
// inverse DFT of the same h0(k) at the same time t, for all 8 fields
// (Dx, Dz, h, Dxz | Sx, Sz, Dxx, Dzz) of the last cascade (float32 raw FFT
// output), and the half-float combine outputs of every cascade.
import * as THREE from 'three';
import { buildCascadeSpectra, DEFAULT_SPECTRUM_PARAMS, significantWaveHeight } from '/src/lib/three/ocean/spectrum.ts';
import { OceanSimulation } from '/src/lib/three/ocean/simulation.ts';

function cpuFields(spec, n, t) {
  // h~(k,t) = h0(k) e^{-iωt} + conj(h0(-k)) e^{+iωt}
  const fields = Array.from({ length: 8 }, () => ({ re: new Float64Array(n * n), im: new Float64Array(n * n) }));
  for (let i = 0; i < n * n; i++) {
    const [ar, ai, br, bi] = [spec.h0[i * 4], spec.h0[i * 4 + 1], spec.h0[i * 4 + 2], spec.h0[i * 4 + 3]];
    const [kx, kz, ik, w] = [spec.wave[i * 4], spec.wave[i * 4 + 1], spec.wave[i * 4 + 2], spec.wave[i * 4 + 3]];
    const c = Math.cos(w * t);
    const s = Math.sin(w * t);
    const hr = ar * c + ai * s + br * c - bi * s;
    const hi = ai * c - ar * s + bi * c + br * s;
    // i·h
    const ihr = -hi;
    const ihi = hr;
    const factors = [
      [ihr * kx * ik, ihi * kx * ik], // Dx
      [ihr * kz * ik, ihi * kz * ik], // Dz
      [hr, hi], // h
      [-hr * kx * kz * ik, -hi * kx * kz * ik], // Dxz
      [ihr * kx, ihi * kx], // Sx
      [ihr * kz, ihi * kz], // Sz
      [-hr * kx * kx * ik, -hi * kx * kx * ik], // Dxx
      [-hr * kz * kz * ik, -hi * kz * kz * ik], // Dzz
    ];
    factors.forEach(([r, im], f) => {
      fields[f].re[i] = r;
      fields[f].im[i] = im;
    });
  }
  const cos = new Float64Array(n);
  const sin = new Float64Array(n);
  for (let j = 0; j < n; j++) {
    cos[j] = Math.cos((2 * Math.PI * j) / n);
    sin[j] = Math.sin((2 * Math.PI * j) / n);
  }
  // Separable inverse DFT (e^{+}), unnormalised. Returns the real part.
  return fields.map(({ re, im }) => {
    const tr = new Float64Array(n * n);
    const ti = new Float64Array(n * n);
    for (let m = 0; m < n; m++) {
      for (let x = 0; x < n; x++) {
        let sr = 0;
        let si = 0;
        for (let k = 0; k < n; k++) {
          const idx = (k * x) % n;
          const vr = re[m * n + k];
          const vi = im[m * n + k];
          sr += vr * cos[idx] - vi * sin[idx];
          si += vr * sin[idx] + vi * cos[idx];
        }
        tr[m * n + x] = sr;
        ti[m * n + x] = si;
      }
    }
    const out = new Float64Array(n * n);
    let maxImag = 0;
    for (let z = 0; z < n; z++) {
      for (let x = 0; x < n; x++) {
        let sr = 0;
        let si = 0;
        for (let k = 0; k < n; k++) {
          const idx = (k * z) % n;
          const vr = tr[k * n + x];
          const vi = ti[k * n + x];
          sr += vr * cos[idx] - vi * sin[idx];
          si += vr * sin[idx] + vi * cos[idx];
        }
        out[z * n + x] = sr;
        maxImag = Math.max(maxImag, Math.abs(si));
      }
    }
    out.maxImag = maxImag;
    return out;
  });
}

function compare(gpu, channel, ref, scale = 1) {
  let maxErr = 0;
  let sumSq = 0;
  for (let i = 0; i < ref.length; i++) {
    const v = ref[i] * scale;
    sumSq += v * v;
    maxErr = Math.max(maxErr, Math.abs(gpu[i * 4 + channel] - v));
  }
  const rms = Math.sqrt(sumSq / ref.length);
  return { rms, maxErr, rel: rms > 0 ? maxErr / rms : maxErr };
}

export function verifyOceanFft({ resolution = 64, time = 3.7, lambda = 0.9, fftPrecision = 'half' } = {}) {
  const renderer = new THREE.WebGLRenderer();
  const params = { ...DEFAULT_SPECTRUM_PARAMS, resolution };
  const spectra = buildCascadeSpectra(params);
  const sim = new OceanSimulation(renderer, spectra, resolution, { fftPrecision });
  const report = { resolution, fftType: sim.fftType === THREE.FloatType ? 'float' : 'half', cascades: [] };
  try {
    sim.lambda = lambda;
    sim.update(time, 0);
    const names = ['Dx', 'Dz', 'h', 'Dxz', 'Sx', 'Sz', 'Dxx', 'Dzz'];
    let heightVar = 0;
    spectra.forEach((spec, c) => {
      const ref = cpuFields(spec, resolution, time);
      const entry = { length: spec.length, raw: null, combined: {} };
      if (c === spectra.length - 1) {
        const f0 = sim.readback(c, 'fft0');
        const f1 = sim.readback(c, 'fft1');
        entry.raw = {};
        names.forEach((name, f) => {
          entry.raw[name] = compare(f < 4 ? f0 : f1, f % 4, ref[f]);
        });
      }
      const disp = sim.readback(c, 'displacement');
      const deriv = sim.readback(c, 'derivatives');
      entry.combined = {
        Dx: compare(disp, 0, ref[0], lambda),
        h: compare(disp, 1, ref[2]),
        Dz: compare(disp, 2, ref[1], lambda),
        Sx: compare(deriv, 0, ref[4]),
        Sz: compare(deriv, 1, ref[5]),
        Dxx: compare(deriv, 2, ref[6], lambda),
        Dzz: compare(deriv, 3, ref[7], lambda),
      };
      entry.maxImag = Math.max(...ref.map((r) => r.maxImag));
      heightVar += entry.combined.h.rms ** 2;
      report.cascades.push(entry);
    });
    report.hsMeasured = 4 * Math.sqrt(heightVar);
    report.hsSpectrum = significantWaveHeight(params);
    const worst = report.cascades.flatMap((e) => [
      ...Object.values(e.raw ?? {}).map((r) => ({ ...r, tol: report.fftType === 'float' ? 1e-3 : 5e-2 })),
      ...Object.values(e.combined).map((r) => ({ ...r, tol: 5e-2 })),
    ]);
    report.ok = worst.every((r) => r.rel <= r.tol);
    return report;
  } finally {
    sim.dispose();
    renderer.dispose();
  }
}
