// Cube DDS decoder for the WG PMREM reflection probes
// (content/environment/.../main_probe.dds), built by the producer's
// `wows-build-environment-library`. Unlike `dds_worker.ts` (which parses a
// single 2D image / face 0 only), this slices all 6 cube faces and decodes
// the sharp top mip (mip 0) of each to linear Float32 RGBA. That top mip is
// the full-res environment radiance; the IBL path re-prefilters it through
// PMREMGenerator so the roughness->mip mapping matches Three's sampler (see
// three/env_ibl.ts).
//
// Two on-disk formats coexist (see project memory / wg_render_pmrem_ibl.md):
//   • DX10 BC6H_UF16 / SF16 (dxgi 95 / 96)  — block-compressed HDR.
//   • DX10 R16G16B16A16_FLOAT (dxgi 10)      — uncompressed half-float.
//   • legacy D3D9 A16B16G16R16F (fourcc 0x71)— uncompressed half-float.
// A naive DX10-only reader misses the 0x71 path, which is the common case for
// older spaces (e.g. 14_Atlantic), so both are handled here.

import { decodeBc6hToRgbaFloat } from './bc6h';

const DDS_MAGIC = 0x20534444; // "DDS "
const DDS_DX10_FOURCC = 0x30315844; // "DX10"
const FOURCC_A16B16G16R16F = 0x71; // legacy D3DFMT_A16B16G16R16F (113)
const DDSCAPS2_CUBEMAP = 0x200;
const D3D11_MISC_TEXTURECUBE = 0x4;

export interface CubeFacesFloat {
  /** Edge length in texels (faces are square). */
  size: number;
  /** 6 faces in DDS / GL order (+X, -X, +Y, -Y, +Z, -Z), each a row-major
   *  Float32 RGBA buffer of `size * size * 4`. Linear HDR radiance. */
  faces: Float32Array[];
}

function halfToFloat(h: number): number {
  const s = (h & 0x8000) >> 15;
  const e = (h & 0x7c00) >> 10;
  const f = h & 0x03ff;
  if (e === 0) return (s ? -1 : 1) * Math.pow(2, -14) * (f / 1024);
  if (e === 0x1f) return f ? NaN : (s ? -1 : 1) * Infinity;
  return (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + f / 1024);
}

/** Decode one half-float RGBA mip (8 bytes/texel, R,G,B,A order) to Float32. */
function decodeHalfRgba(view: DataView, byteOffset: number, w: number, h: number): Float32Array {
  const out = new Float32Array(w * h * 4);
  let o = byteOffset;
  for (let i = 0; i < w * h * 4; i++) {
    out[i] = halfToFloat(view.getUint16(o, true));
    o += 2;
  }
  return out;
}

type FaceCodec = {
  /** bytes one mip level occupies on disk. */
  levelBytes: (w: number, h: number) => number;
  /** decode one mip's bytes (sliced view) to Float32 RGBA. */
  decode: (view: DataView, buf: ArrayBuffer, off: number, w: number, h: number) => Float32Array;
};

/**
 * Decode a 6-face cube DDS into per-face Float32 RGBA (mip 0 only).
 * Returns null if the buffer isn't a recognised cube DDS.
 */
export function decodeCubeDds(buf: ArrayBuffer): CubeFacesFloat | null {
  if (buf.byteLength < 128) return null;
  const view = new DataView(buf);
  if (view.getUint32(0, true) !== DDS_MAGIC) return null;

  const height = view.getUint32(12, true);
  const width = view.getUint32(16, true);
  const mipCount = Math.max(1, view.getUint32(28, true));
  const caps2 = view.getUint32(112, true);
  const fourCC = view.getUint32(84, true);

  let dataStart: number;
  let isCube = (caps2 & DDSCAPS2_CUBEMAP) !== 0;
  let codec: FaceCodec | null = null;

  if (fourCC === DDS_DX10_FOURCC) {
    if (buf.byteLength < 148) return null;
    const dxgi = view.getUint32(128, true);
    const misc = view.getUint32(136, true);
    isCube = isCube || (misc & D3D11_MISC_TEXTURECUBE) !== 0;
    dataStart = 148;
    if (dxgi === 95 || dxgi === 96) {
      const signed = dxgi === 96;
      codec = {
        levelBytes: (w, h) => Math.max(1, Math.ceil(w / 4)) * Math.max(1, Math.ceil(h / 4)) * 16,
        decode: (_v, b, off, w, h) =>
          decodeBc6hToRgbaFloat(new Uint8Array(b, off, codec!.levelBytes(w, h)), w, h, signed),
      };
    } else if (dxgi === 10) {
      codec = {
        levelBytes: (w, h) => w * h * 8,
        decode: (v, _b, off, w, h) => decodeHalfRgba(v, off, w, h),
      };
    }
  } else if (fourCC === FOURCC_A16B16G16R16F) {
    dataStart = 128;
    codec = {
      levelBytes: (w, h) => w * h * 8,
      decode: (v, _b, off, w, h) => decodeHalfRgba(v, off, w, h),
    };
  } else {
    return null;
  }

  if (!codec || !isCube) return null;

  // Each face stores its full mip chain sequentially; we only need mip 0.
  let faceStride = 0;
  {
    let w = width;
    let h = height;
    for (let i = 0; i < mipCount; i++) {
      faceStride += codec.levelBytes(w, h);
      if (w === 1 && h === 1) break;
      w = Math.max(1, w >> 1);
      h = Math.max(1, h >> 1);
    }
  }

  const faces: Float32Array[] = [];
  for (let f = 0; f < 6; f++) {
    const off = dataStart + f * faceStride;
    if (off + codec.levelBytes(width, height) > buf.byteLength) return null;
    faces.push(codec.decode(view, buf, off, width, height));
  }

  return { size: width, faces };
}
