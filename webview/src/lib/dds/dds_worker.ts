// Off-main-thread DDS parser. Receives a raw ArrayBuffer, dispatches
// to the BC7 / BC4 / classic DXT path, and posts back the parsed mip
// chain with each mip's bytes living in its own (transferable) buffer.
//
// BC4 software-decode (the only truly CPU-heavy parse) happens here so
// the render thread stays responsive during texture toggles. Output
// `format` is the numeric THREE constant; the main thread reconstructs
// the CompressedTexture / DataTexture from the response. Worker bundle
// does not import three.js — DXT classic parse is inlined.

const DDS_MAGIC = 0x20534444;
const DDS_DX10_FOURCC = 0x30315844;

const FOURCC_DXT1 = 0x31545844;
const FOURCC_DXT3 = 0x33545844;
const FOURCC_DXT5 = 0x35545844;

const DDPF_ALPHAPIXELS = 0x1;

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
  data: Uint8Array;
  width: number;
  height: number;
}

export interface ParseSuccess {
  kind: 'bptc' | 'rgtc' | 'classic';
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

function parse(buf: ArrayBuffer): ParseSuccess | null {
  const view = new DataView(buf);
  if (view.byteLength < 128 || view.getUint32(0, true) !== DDS_MAGIC) return null;
  const fourCC = view.getUint32(84, true);
  if (fourCC === DDS_DX10_FOURCC) {
    if (buf.byteLength < 148) return null;
    const dxgi = view.getUint32(128, true);
    if (BPTC_DXGI[dxgi]) return parseBptc(buf);
    if (RGTC_DXGI[dxgi]) return parseRgtc(buf);
    if (CLASSIC_DXGI[dxgi]) return parseDx10Classic(buf, dxgi);
    return null;
  }
  return parseClassic(buf);
}

const ctx = self as unknown as Worker;

ctx.onmessage = (e: MessageEvent<DecodeRequest>) => {
  const { id, buffer } = e.data;
  try {
    const result = parse(buffer);
    if (!result) {
      const resp: DecodeResponse = { id, ok: false, error: 'unsupported or malformed DDS' };
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
