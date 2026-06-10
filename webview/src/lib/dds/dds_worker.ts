// Off-main-thread DDS parser. Receives a raw ArrayBuffer, dispatches
// to the BC7 / BC4 / classic DXT path, and posts back the parsed mip
// chain with each mip's bytes living in its own (transferable) buffer.
//
// BC4 software-decode (the only truly CPU-heavy parse) happens here so
// the render thread stays responsive during texture toggles. Output
// `format` is the numeric THREE constant; the main thread reconstructs
// the CompressedTexture / DataTexture from the response. Worker bundle
// does not import three.js — DXT classic parse is inlined.

import { decodeBc6hToRgbaFloat } from './bc6h';

const DDS_MAGIC = 0x20534444;
const DDS_DX10_FOURCC = 0x30315844;

const FOURCC_DXT1 = 0x31545844;
const FOURCC_DXT3 = 0x33545844;
const FOURCC_DXT5 = 0x35545844;
const FOURCC_G16R16F = 0x70; // legacy D3DFMT_G16R16F (112)

const DDPF_ALPHAPIXELS = 0x1;
const DDPF_FOURCC = 0x4;
const DDPF_RGB = 0x40;

// THREE format constants (mirrored as numerics so the worker doesn't
// pull three.js into the bundle).
const RGBAFormat = 1023;
const RGB_S3TC_DXT1_Format = 33776;
const RGBA_S3TC_DXT1_Format = 33777;
const RGBA_S3TC_DXT3_Format = 33778;
const RGBA_S3TC_DXT5_Format = 33779;
const RGBA_BPTC_Format = 36492;

const BPTC_DXGI: Record<number, { sRGB: boolean }> = {
  98: { sRGB: false }, // BC7_UNORM
  99: { sRGB: true }, // BC7_UNORM_SRGB
};

const RGTC_DXGI: Record<number, { signed: boolean }> = {
  80: { signed: false }, // BC4_UNORM
  81: { signed: true }, // BC4_SNORM
};

// BC6H HDR formats. Decoded in software (no widely-available WebGL float-BPTC
// sampling) to a Float32 RGBA DataTexture. UF16 (95) is the live case — WG's
// fire/smoke colour ramps (particles/ramps/*_HDR.dds); SF16 (96) handled too.
const BC6H_DXGI: Record<number, { signed: boolean }> = {
  95: { signed: false }, // BC6H_UF16
  96: { signed: true }, // BC6H_SF16
};

// DX10-wrapped classic BC formats. WG's toolkit emits some `_mr.dds`
// files this way: DDS fourCC = "DX10", DXGI format = 71 (BC1_UNORM)
// rather than the older fourCC = "DXT1" header. Same block layout,
// just a different way of advertising it.
const CLASSIC_DXGI: Record<number, { format: number; blockSize: number; sRGB: boolean }> = {
  71: { format: RGB_S3TC_DXT1_Format, blockSize: 8, sRGB: false }, // BC1_UNORM
  72: { format: RGB_S3TC_DXT1_Format, blockSize: 8, sRGB: true }, // BC1_UNORM_SRGB
  74: { format: RGBA_S3TC_DXT3_Format, blockSize: 16, sRGB: false }, // BC2_UNORM
  75: { format: RGBA_S3TC_DXT3_Format, blockSize: 16, sRGB: true }, // BC2_UNORM_SRGB
  77: { format: RGBA_S3TC_DXT5_Format, blockSize: 16, sRGB: false }, // BC3_UNORM
  78: { format: RGBA_S3TC_DXT5_Format, blockSize: 16, sRGB: true }, // BC3_UNORM_SRGB
};

interface Mip {
  // Uint8Array for compressed/8-bit paths; Float32Array for HDR/half-float
  // ('rgbaf') paths where each pixel is 4 float32 (R,G,B,A=1).
  data: Uint8Array | Float32Array;
  width: number;
  height: number;
}

export interface ParseSuccess {
  kind: 'bptc' | 'rgtc' | 'classic' | 'rgba8' | 'rgbaf';
  format: number;
  mipmaps: Mip[];
  width: number;
  height: number;
  sRGBOverride?: boolean;
}

export interface DecodeRequest {
  id: number;
  buffer: ArrayBuffer;
}

export type DecodeResponse =
  | { id: number; ok: true; result: ParseSuccess }
  | { id: number; ok: false; error: string };

function parseBptc(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (buf.byteLength < 148) return null;
  const dxgi = view.getUint32(128, true);
  const desc = BPTC_DXGI[dxgi];
  if (!desc) return null;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 148;
  const mipmaps: Mip[] = [];
  for (let i = 0; i < mipCount; i++) {
    const blockW = Math.max(1, Math.ceil(width / 4));
    const blockH = Math.max(1, Math.ceil(height / 4));
    const byteSize = blockW * blockH * 16;
    if (offset + byteSize > buf.byteLength) break;
    // Copy into a fresh ArrayBuffer for zero-copy transfer back.
    const out = new Uint8Array(byteSize);
    out.set(new Uint8Array(buf, offset, byteSize));
    mipmaps.push({ data: out, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return {
    kind: 'bptc',
    format: RGBA_BPTC_Format,
    mipmaps,
    width: topW,
    height: topH,
    sRGBOverride: desc.sRGB,
  };
}

function decodeBc4Block(
  buf: Uint8Array,
  offset: number,
  signed: boolean,
  outValues: Uint8Array,
): void {
  const r0 = signed ? new Int8Array(buf.buffer, buf.byteOffset + offset, 1)[0] : buf[offset];
  const r1 = signed
    ? new Int8Array(buf.buffer, buf.byteOffset + offset + 1, 1)[0]
    : buf[offset + 1];
  const table = new Float32Array(8);
  table[0] = r0;
  table[1] = r1;
  if (r0 > r1) {
    for (let i = 1; i <= 6; i++) table[i + 1] = ((7 - i) * r0 + i * r1) / 7;
  } else {
    for (let i = 1; i <= 4; i++) table[i + 1] = ((5 - i) * r0 + i * r1) / 5;
    table[6] = signed ? -127 : 0;
    table[7] = signed ? 127 : 255;
  }
  const lo = buf[offset + 2] | (buf[offset + 3] << 8) | (buf[offset + 4] << 16);
  const hi = buf[offset + 5] | (buf[offset + 6] << 8) | (buf[offset + 7] << 16);
  for (let p = 0; p < 16; p++) {
    let idx: number;
    if (p < 8) idx = (lo >> (p * 3)) & 7;
    else idx = (hi >> ((p - 8) * 3)) & 7;
    let v = Math.round(table[idx]);
    if (signed) v = Math.max(-127, Math.min(127, v)) + 128;
    else v = Math.max(0, Math.min(255, v));
    outValues[p] = v;
  }
}

function decodeBc4ToRgba(
  blockData: Uint8Array,
  width: number,
  height: number,
  signed: boolean,
): Uint8Array {
  const out = new Uint8Array(width * height * 4);
  const blockW = Math.max(1, Math.ceil(width / 4));
  const blockH = Math.max(1, Math.ceil(height / 4));
  const blockValues = new Uint8Array(16);
  for (let by = 0; by < blockH; by++) {
    for (let bx = 0; bx < blockW; bx++) {
      const blockIdx = by * blockW + bx;
      decodeBc4Block(blockData, blockIdx * 8, signed, blockValues);
      for (let py = 0; py < 4; py++) {
        const y = by * 4 + py;
        if (y >= height) break;
        for (let px = 0; px < 4; px++) {
          const x = bx * 4 + px;
          if (x >= width) break;
          const v = blockValues[py * 4 + px];
          const oi = (y * width + x) * 4;
          out[oi] = v;
          out[oi + 1] = 0;
          out[oi + 2] = 0;
          out[oi + 3] = 255;
        }
      }
    }
  }
  return out;
}

function parseRgtc(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (buf.byteLength < 148) return null;
  const dxgi = view.getUint32(128, true);
  const desc = RGTC_DXGI[dxgi];
  if (!desc) return null;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 148;
  const mipmaps: Mip[] = [];
  for (let i = 0; i < mipCount; i++) {
    const blockW = Math.max(1, Math.ceil(width / 4));
    const blockH = Math.max(1, Math.ceil(height / 4));
    const byteSize = blockW * blockH * 8;
    if (offset + byteSize > buf.byteLength) break;
    const blockData = new Uint8Array(buf, offset, byteSize);
    const rgba = decodeBc4ToRgba(blockData, width, height, desc.signed);
    mipmaps.push({ data: rgba, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return {
    kind: 'rgtc',
    format: RGBAFormat,
    mipmaps,
    width: topW,
    height: topH,
  };
}

// DX10-wrapped BC6H (DXGI 95 UF16 / 96 SF16). Same DDS header layout + mip
// loop as parseRgtc, but each 4x4 block is 16 bytes and we software-decode it
// to a Float32 RGBA buffer (HDR; no alpha → A=1). Returned as kind 'rgbaf'
// for the main thread to wrap in a FloatType DataTexture.
function parseBc6h(buf: ArrayBuffer, signed: boolean): ParseSuccess | null {
  const view = new DataView(buf);
  if (buf.byteLength < 148) return null;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 148;
  const mipmaps: Mip[] = [];
  for (let i = 0; i < mipCount; i++) {
    const blockW = Math.max(1, Math.ceil(width / 4));
    const blockH = Math.max(1, Math.ceil(height / 4));
    const byteSize = blockW * blockH * 16;
    if (offset + byteSize > buf.byteLength) break;
    const blockData = new Uint8Array(buf, offset, byteSize);
    const rgbaf = decodeBc6hToRgbaFloat(blockData, width, height, signed);
    mipmaps.push({ data: rgbaf, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return {
    kind: 'rgbaf',
    format: RGBAFormat,
    mipmaps,
    width: topW,
    height: topH,
  };
}

function halfToFloat(h: number): number {
  const s = (h & 0x8000) >> 15;
  const e = (h & 0x7c00) >> 10;
  const f = h & 0x03ff;
  if (e === 0) return (s ? -1 : 1) * Math.pow(2, -14) * (f / 1024);
  if (e === 0x1f) return f ? NaN : (s ? -1 : 1) * Infinity;
  return (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + f / 1024);
}

// Legacy D3D9 G16R16F (fourCC 0x70): two half-float channels per texel.
// WG uses this for particle water-deformation sprites
// (particles/deform16f/*.dds). Expand to Float32 RGBA for a normal Three.js
// DataTexture: R/G carry the authored channels, B=0, A=1.
function parseLegacyG16R16F(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (buf.byteLength < 128) return null;
  const pfFlags = view.getUint32(80, true);
  const fourCC = view.getUint32(84, true);
  if ((pfFlags & DDPF_FOURCC) === 0 || fourCC !== FOURCC_G16R16F) return null;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 128;
  const mipmaps: Mip[] = [];
  for (let i = 0; i < mipCount; i++) {
    const pixelCount = width * height;
    const byteSize = pixelCount * 4;
    if (offset + byteSize > buf.byteLength) break;
    const out = new Float32Array(pixelCount * 4);
    let o = offset;
    for (let p = 0; p < pixelCount; p++) {
      const dst = p * 4;
      out[dst] = halfToFloat(view.getUint16(o, true));
      out[dst + 1] = halfToFloat(view.getUint16(o + 2, true));
      out[dst + 2] = 0;
      out[dst + 3] = 1;
      o += 4;
    }
    mipmaps.push({ data: out, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return { kind: 'rgbaf', format: RGBAFormat, mipmaps, width: topW, height: topH };
}

// Classic non-DX10 DDS: reads DDS_HEADER, picks format from fourCC, slices
// mips into fresh Uint8Arrays. Covers DXT1 / DXT3 / DXT5 (the only classic
// codecs the WoWS pipeline emits). The DDPF_ALPHAPIXELS flag picks the
// DXT1 RGBA vs RGB variant the way three.js's DDSLoader does.
function parseClassic(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (buf.byteLength < 128) return null;
  const fourCC = view.getUint32(84, true);
  if (fourCC === DDS_DX10_FOURCC) return null;

  const pfFlags = view.getUint32(80, true);
  const hasAlpha = (pfFlags & DDPF_ALPHAPIXELS) !== 0;
  let format: number;
  let blockSize: number;
  if (fourCC === FOURCC_DXT1) {
    format = hasAlpha ? RGBA_S3TC_DXT1_Format : RGB_S3TC_DXT1_Format;
    blockSize = 8;
  } else if (fourCC === FOURCC_DXT3) {
    format = RGBA_S3TC_DXT3_Format;
    blockSize = 16;
  } else if (fourCC === FOURCC_DXT5) {
    format = RGBA_S3TC_DXT5_Format;
    blockSize = 16;
  } else {
    return null;
  }

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 128;
  const mipmaps: Mip[] = [];
  for (let i = 0; i < mipCount; i++) {
    const blockW = Math.max(1, Math.ceil(width / 4));
    const blockH = Math.max(1, Math.ceil(height / 4));
    const byteSize = blockW * blockH * blockSize;
    if (offset + byteSize > buf.byteLength) break;
    const out = new Uint8Array(byteSize);
    out.set(new Uint8Array(buf, offset, byteSize));
    mipmaps.push({ data: out, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return { kind: 'classic', format, mipmaps, width: topW, height: topH };
}

// DX10-wrapped classic BC1/BC2/BC3. Same block math as `parseClassic`
// but the pixel data starts at byte 148 (128 + 20-byte DX10 extension
// header) and the format comes from the DXGI table rather than the
// classic fourCC.
function parseDx10Classic(buf: ArrayBuffer, dxgi: number): ParseSuccess | null {
  const desc = CLASSIC_DXGI[dxgi];
  if (!desc) return null;
  const view = new DataView(buf);
  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 148;
  const mipmaps: Mip[] = [];
  for (let i = 0; i < mipCount; i++) {
    const blockW = Math.max(1, Math.ceil(width / 4));
    const blockH = Math.max(1, Math.ceil(height / 4));
    const byteSize = blockW * blockH * desc.blockSize;
    if (offset + byteSize > buf.byteLength) break;
    const out = new Uint8Array(byteSize);
    out.set(new Uint8Array(buf, offset, byteSize));
    mipmaps.push({ data: out, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return {
    kind: 'classic',
    format: desc.format,
    mipmaps,
    width: topW,
    height: topH,
    sRGBOverride: desc.sRGB,
  };
}

// Uncompressed RGBA/BGRA 8-bit per channel. WG ships particle color
// ramps (e.g. ``particles/ramps/fire_yellow_2.dds``) as small
// 256x1..256x8 uncompressed textures — the GRADIENT_MAP particle blend
// path keys these as 1D LUTs via the fragment shader. Pixel-format
// signature: DDPF_RGB | DDPF_ALPHAPIXELS, 32 bpp, channel masks for
// either RGBA or BGRA byte order.
function parseUncompressedRgba8(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (buf.byteLength < 128) return null;
  const pfFlags = view.getUint32(80, true);
  if ((pfFlags & DDPF_RGB) === 0) return null;
  // Non-DXT non-DX10: fourCC must be 0 (no compression).
  const fourCC = view.getUint32(84, true);
  if (fourCC !== 0 && (pfFlags & DDPF_FOURCC) !== 0) return null;
  const rgbBits = view.getUint32(88, true);
  if (rgbBits !== 32) return null; // 24bpp + 16bpp paths not needed yet.

  const maskR = view.getUint32(92, true);
  const maskG = view.getUint32(96, true);
  const maskB = view.getUint32(100, true);
  const maskA = view.getUint32(104, true);

  // Detect channel order from the masks. R=0xff means R is the low
  // byte (RGBA layout); R=0xff0000 means R is the high byte (BGRA).
  let swizzle: 'rgba' | 'bgra';
  if (maskR === 0x000000ff && maskG === 0x0000ff00 && maskB === 0x00ff0000) {
    swizzle = 'rgba';
  } else if (maskR === 0x00ff0000 && maskG === 0x0000ff00 && maskB === 0x000000ff) {
    swizzle = 'bgra';
  } else {
    return null; // unsupported mask layout
  }
  const hasAlpha = (pfFlags & DDPF_ALPHAPIXELS) !== 0 && maskA !== 0;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const topW = width;
  const topH = height;
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 128;
  const mipmaps: Mip[] = [];
  const src = new Uint8Array(buf);
  for (let i = 0; i < mipCount; i++) {
    const pixelCount = width * height;
    const byteSize = pixelCount * 4;
    if (offset + byteSize > buf.byteLength) break;
    const out = new Uint8Array(byteSize);
    if (swizzle === 'rgba') {
      out.set(src.subarray(offset, offset + byteSize));
      if (!hasAlpha) {
        // Force alpha=255 when the file doesn't carry an alpha channel.
        for (let p = 3; p < byteSize; p += 4) out[p] = 0xff;
      }
    } else {
      // BGRA on disk -> swap R/B into RGBA output.
      for (let p = 0; p < byteSize; p += 4) {
        out[p] = src[offset + p + 2];
        out[p + 1] = src[offset + p + 1];
        out[p + 2] = src[offset + p];
        out[p + 3] = hasAlpha ? src[offset + p + 3] : 0xff;
      }
    }
    mipmaps.push({ data: out, width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;
  return { kind: 'rgba8', format: RGBAFormat, mipmaps, width: topW, height: topH };
}

// DXGI formats the worker recognises by name but can't decode in software.
// Used only by describeFailure to give a precise "format X not implemented"
// message. BC6H (95/96) was here previously and is now decoded — see
// parseBc6h / ./bc6h.ts — so the table is currently empty. Kept as the hook
// for the next unsupported DXGI code we encounter.
const UNSUPPORTED_DXGI: Record<number, string> = {};

function parse(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (view.byteLength < 128 || view.getUint32(0, true) !== DDS_MAGIC) return null;
  const fourCC = view.getUint32(84, true);
  if (fourCC === DDS_DX10_FOURCC) {
    if (buf.byteLength < 148) return null;
    const dxgi = view.getUint32(128, true);
    if (BPTC_DXGI[dxgi]) return parseBptc(buf);
    if (RGTC_DXGI[dxgi]) return parseRgtc(buf);
    if (BC6H_DXGI[dxgi]) return parseBc6h(buf, BC6H_DXGI[dxgi].signed);
    if (CLASSIC_DXGI[dxgi]) return parseDx10Classic(buf, dxgi);
    return null;
  }
  // Try the BC1/3/5 classic-fourCC path first; fall through to the
  // uncompressed RGBA8 reader for files like fire_yellow_*.dds.
  const classic = parseClassic(buf);
  if (classic) return classic;
  const g16r16f = parseLegacyG16R16F(buf);
  if (g16r16f) return g16r16f;
  return parseUncompressedRgba8(buf);
}

function describeFailure(buf: ArrayBuffer): string {
  const view = new DataView(buf);
  if (view.byteLength < 128 || view.getUint32(0, true) !== DDS_MAGIC) {
    return 'not a DDS file';
  }
  const fourCC = view.getUint32(84, true);
  if (fourCC === DDS_DX10_FOURCC && buf.byteLength >= 148) {
    const dxgi = view.getUint32(128, true);
    const name = UNSUPPORTED_DXGI[dxgi];
    if (name) return `DXGI ${dxgi} (${name}) not implemented by worker`;
    return `unsupported DXGI format ${dxgi}`;
  }
  if (fourCC !== 0) return `unsupported classic fourCC 0x${fourCC.toString(16)}`;
  return 'unsupported or malformed DDS';
}

const ctx = self as unknown as Worker;

ctx.onmessage = (e: MessageEvent<DecodeRequest>) => {
  const { id, buffer } = e.data;
  try {
    const result = parse(buffer);
    if (!result) {
      const resp: DecodeResponse = {
        id, ok: false, error: describeFailure(buffer),
      };
      ctx.postMessage(resp);
      return;
    }
    const transfers = result.mipmaps.map((m) => m.data.buffer);
    const resp: DecodeResponse = { id, ok: true, result };
    ctx.postMessage(resp, transfers);
  } catch (err) {
    const resp: DecodeResponse = { id, ok: false, error: String(err) };
    ctx.postMessage(resp);
  }
};
