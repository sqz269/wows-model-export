// DDS loading helpers for the library + ship viewers.
//
// Three.js's bundled DDSLoader handles DXT1/3/5 + a small subset of
// DX10-tagged DDS (BC4/5). WG normal maps are BC7 (DXGI 98) which the
// loader throws on; we add a thin BC7 fast-path that bypasses
// DDSLoader and feeds the compressed block data straight to the GPU
// via `EXT_texture_compression_bptc` (WebGL2 baseline). Falls back to
// skipping (with a warning) on UAs that don't expose the extension.
//
// BC4 (RGTC, used for the toolkit's `_nbmask.dd?` no-camo mask) is
// software-decoded to a DataTexture to dodge a three.js r0.165 bug
// where the `RED_RGTC1_Format` upload path silently zero-samples.

import * as THREE from 'three';
import { DDSLoader } from 'three/addons/loaders/DDSLoader.js';

/** Mip chain entry — a single `.dd0` / `.dd1` / `.dd2` / `.dds` path. */
export type DdsMipPaths = string[];

const DDS_MAGIC = 0x20534444; // "DDS "
const DDS_DX10_FOURCC = 0x30315844; // "DX10"

// DXGI formats DDSLoader actually handles. BC4/BC5 (80/81/83/84) are NOT
// handled by the bundled DDSLoader (its DX10 switch only knows BC6H);
// BC4 routes through our own RGTC parser. BC5 isn't used by the pipeline
// — left in the allowlist as a no-op on the off chance one appears.
const SUPPORTED_DXGI = new Set<number>([
  83, // BC5_UNORM
  84, // BC5_SNORM
]);

// DXGI BC7 formats we route through our own parser (DDSLoader rejects them).
// WG ships normal maps (`*_n.dd0`) and many albedos in BC7. BC6H (HDR float)
// isn't currently used by WG ship textures.
const BPTC_DXGI: Record<number, { sRGB: boolean }> = {
  98: { sRGB: false }, // BC7_UNORM
  99: { sRGB: true }, // BC7_UNORM_SRGB
};

// DXGI BC4 formats. Toolkit emits these for `_nbmask.dds` (categorical
// camo gate; single-channel is the natural packing).
const RGTC_DXGI: Record<number, { signed: boolean }> = {
  80: { signed: false }, // BC4_UNORM
  81: { signed: true }, // BC4_SNORM
};

/**
 * Pick the full mip chain (`.dds`) when present, otherwise the first entry.
 * Single-URL form. Use {@link resolveDdsMipUrls} for the mip-chain loader.
 */
export function pickDdsUrl(urls: DdsMipPaths | undefined, base: string): string | null {
  if (!urls || urls.length === 0) return null;
  const full = urls.find((u) => u.toLowerCase().endsWith('.dds'));
  const rel = full ?? urls[0];
  if (!rel) return null;
  try {
    return new URL(rel, base).toString();
  } catch {
    return rel;
  }
}

/**
 * Resolve every available mip-level URL in level order (highest-res first).
 * Returns absolute URLs ready for fetch. The toolkit emits `.dd0` (level 0),
 * `.dd1`, `.dd2`, `.dds` (bundled mip tail). When all four are present, the
 * consumer can stitch them into one full mip chain via {@link loadDdsMipChain}.
 *
 * Inputs without a `.dd0` (typical for accessory libraries) get exactly one
 * URL — the `.dds` file alone — which still has its own embedded mip chain.
 *
 * Returns `[]` for known placeholders (e.g. `default_ao.dds`, a 16×16 uniform
 * mid-gray dummy bound to ~94% of accessory library assets by the toolkit).
 * Binding the placeholder uniformly dims indirect lighting with no
 * per-pixel signal — we'd rather have no aoMap and let Three.js fall back
 * to the default (1.0 = no occlusion).
 */
export function resolveDdsMipUrls(urls: DdsMipPaths | undefined, base: string): string[] {
  if (!urls || urls.length === 0) return [];
  if (urls.some((u) => isPlaceholderDdsName(u))) return [];
  const order = (u: string): number => {
    const lower = u.toLowerCase();
    if (lower.endsWith('.dd0')) return 0;
    if (lower.endsWith('.dd1')) return 1;
    if (lower.endsWith('.dd2')) return 2;
    if (lower.endsWith('.dds')) return 3;
    return 99;
  };
  const sorted = [...urls].sort((a, b) => order(a) - order(b));
  const resolved: string[] = [];
  for (const u of sorted) {
    try {
      resolved.push(new URL(u, base).toString());
    } catch {
      resolved.push(u);
    }
  }
  return resolved;
}

const PLACEHOLDER_DDS_NAMES = new Set<string>([
  'default_ao.dds',
  'default_ao.dd0',
  'default_ao.dd1',
  'default_ao.dd2',
]);

function isPlaceholderDdsName(p: string): boolean {
  const basename = p.replace(/\\/g, '/').split('/').pop()?.toLowerCase() ?? '';
  return PLACEHOLDER_DDS_NAMES.has(basename);
}

type DdsKind = 'classic' | 'bptc' | 'rgtc' | null;

interface DdsFetchResult {
  buf: ArrayBuffer;
  kind: DdsKind;
}

async function fetchDdsIfSupported(url: string): Promise<DdsFetchResult | null> {
  const resp = await fetch(url);
  if (!resp.ok) return null;
  const buf = await resp.arrayBuffer();
  const view = new DataView(buf);
  if (view.byteLength < 128 || view.getUint32(0, true) !== DDS_MAGIC) return null;
  const fourCC = view.getUint32(84, true);
  if (fourCC === DDS_DX10_FOURCC && buf.byteLength >= 148) {
    const dxgi = view.getUint32(128, true);
    if (BPTC_DXGI[dxgi]) return { buf, kind: 'bptc' };
    if (RGTC_DXGI[dxgi]) return { buf, kind: 'rgtc' };
    if (!SUPPORTED_DXGI.has(dxgi)) {
      console.warn(`[dds] skipping ${url}: DXGI format ${dxgi} not supported`);
      return null;
    }
  }
  return { buf, kind: 'classic' };
}

interface ParsedDds {
  mipmaps: { data: Uint8Array; width: number; height: number }[];
  format: THREE.CompressedPixelFormat;
  width: number;
  height: number;
  sRGBOverride?: boolean;
}

/** Parse a DX10-tagged BC7 DDS into mip-level byte slices. */
function parseBptcDds(buf: ArrayBuffer): ParsedDds | null {
  const view = new DataView(buf);
  if (view.getUint32(0, true) !== DDS_MAGIC) return null;
  if (view.getUint32(84, true) !== DDS_DX10_FOURCC) return null;
  if (buf.byteLength < 148) return null;
  const dxgi = view.getUint32(128, true);
  const desc = BPTC_DXGI[dxgi];
  if (!desc) return null;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 148;
  const mipmaps: ParsedDds['mipmaps'] = [];
  for (let i = 0; i < mipCount; i++) {
    const blockW = Math.max(1, Math.ceil(width / 4));
    const blockH = Math.max(1, Math.ceil(height / 4));
    const byteSize = blockW * blockH * 16;
    if (offset + byteSize > buf.byteLength) break;
    mipmaps.push({ data: new Uint8Array(buf, offset, byteSize), width, height });
    offset += byteSize;
    if (width === 1 && height === 1) break;
    width = Math.max(1, width >> 1);
    height = Math.max(1, height >> 1);
  }
  if (mipmaps.length === 0) return null;

  return {
    mipmaps,
    format: THREE.RGBA_BPTC_Format as unknown as THREE.CompressedPixelFormat,
    width: mipmaps[0].width,
    height: mipmaps[0].height,
    sRGBOverride: desc.sRGB,
  };
}

const _bptcChecked = new WeakSet<THREE.WebGLRenderer>();
function ensureBptcSupport(renderer: THREE.WebGLRenderer): boolean {
  if (_bptcChecked.has(renderer)) return true;
  _bptcChecked.add(renderer);
  const gl = renderer.getContext();
  const ext = gl.getExtension('EXT_texture_compression_bptc');
  if (!ext) {
    console.warn(
      '[dds] EXT_texture_compression_bptc unavailable — BC7 textures ' +
        '(WG normal maps, mat_* atlases) will be skipped.',
    );
    return false;
  }
  return true;
}

const _rgtcChecked = new WeakSet<THREE.WebGLRenderer>();
function ensureRgtcSupport(renderer: THREE.WebGLRenderer): boolean {
  if (_rgtcChecked.has(renderer)) return true;
  _rgtcChecked.add(renderer);
  const gl = renderer.getContext();
  const ext = gl.getExtension('EXT_texture_compression_rgtc');
  if (!ext) {
    console.warn(
      '[dds] EXT_texture_compression_rgtc unavailable — BC4 nbmask textures will be skipped.',
    );
    return false;
  }
  return true;
}

// Decode a single 4×4 BC4 block into 16 uint8 values.
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

// Software-decode a BC4 mip into a flat Uint8Array of RGBA values. Used to
// bypass three.js r0.165's broken RED_RGTC1 upload path (silent zero-sample).
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

interface ParsedRgtcDds {
  mipmaps: { data: Uint8Array; width: number; height: number }[]; // RGBA
  width: number;
  height: number;
}

function parseRgtcDds(buf: ArrayBuffer): ParsedRgtcDds | null {
  const view = new DataView(buf);
  if (view.getUint32(0, true) !== DDS_MAGIC) return null;
  if (view.getUint32(84, true) !== DDS_DX10_FOURCC) return null;
  if (buf.byteLength < 148) return null;
  const dxgi = view.getUint32(128, true);
  const desc = RGTC_DXGI[dxgi];
  if (!desc) return null;

  let height = view.getUint32(12, true);
  let width = view.getUint32(16, true);
  const mipCount = Math.max(1, view.getUint32(28, true));

  let offset = 148;
  const mipmaps: ParsedRgtcDds['mipmaps'] = [];
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
  return { mipmaps, width: mipmaps[0].width, height: mipmaps[0].height };
}

function makeRgtcDataTexture(
  mip: { data: Uint8Array; width: number; height: number },
  renderer: THREE.WebGLRenderer,
): THREE.DataTexture {
  // `as unknown as Uint8Array<ArrayBuffer>` strips the SharedArrayBuffer
  // possibility three's DataTexture rejects; our decode allocates a
  // plain ArrayBuffer-backed Uint8Array so the cast is safe.
  const tex = new THREE.DataTexture(
    mip.data as unknown as Uint8Array<ArrayBuffer>,
    mip.width,
    mip.height,
    THREE.RGBAFormat,
    THREE.UnsignedByteType,
  );
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  tex.flipY = false;
  tex.colorSpace = THREE.NoColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/**
 * Fetch + parse one DDS file into a CompressedTexture. Three.js's GLTFLoader
 * does NOT V-flip UVs the way gltFast does, and WebGL ignores `flipY` on
 * compressed textures — UVs passed through unchanged + DDS bytes unflipped
 * makes the raw DDS atlas land right-side-up under a native sample. Do NOT
 * copy the Unity `scale=(1,-1)` flip here — see
 * `tools/reference/shared/texture_orientation_investigation.md`.
 */
export async function loadDdsTexture(
  url: string,
  sRGB: boolean,
  ddsLoader: DDSLoader,
  renderer: THREE.WebGLRenderer,
): Promise<THREE.Texture | null> {
  const result = await fetchDdsIfSupported(url);
  if (!result) return null;

  if (result.kind === 'rgtc') {
    if (!ensureRgtcSupport(renderer)) return null;
    const parsed = parseRgtcDds(result.buf);
    if (!parsed) {
      console.warn(`[dds] BC4 parse failed for ${url}`);
      return null;
    }
    return makeRgtcDataTexture(parsed.mipmaps[0], renderer);
  }

  let mipmaps: ParsedDds['mipmaps'];
  let format: THREE.CompressedPixelFormat;
  let width: number, height: number;
  let effectiveSRGB = sRGB;

  if (result.kind === 'bptc') {
    if (!ensureBptcSupport(renderer)) return null;
    const parsed = parseBptcDds(result.buf);
    if (!parsed) {
      console.warn(`[dds] BC7 parse failed for ${url}`);
      return null;
    }
    mipmaps = parsed.mipmaps;
    format = parsed.format;
    width = parsed.width;
    height = parsed.height;
    if (parsed.sRGBOverride !== undefined) effectiveSRGB = parsed.sRGBOverride;
  } else {
    let parsed: ReturnType<typeof ddsLoader.parse>;
    try {
      parsed = ddsLoader.parse(result.buf, true);
    } catch (err) {
      console.warn(`[dds] parse failed for ${url}:`, err);
      return null;
    }
    if (!parsed?.mipmaps?.length || !parsed.width || !parsed.height) {
      console.warn(`[dds] ${url} parsed with no usable mips`);
      return null;
    }
    mipmaps = parsed.mipmaps as unknown as ParsedDds['mipmaps'];
    format = parsed.format as THREE.CompressedPixelFormat;
    width = parsed.width;
    height = parsed.height;
  }

  const tex = new THREE.CompressedTexture(mipmaps as unknown as ImageData[], width, height, format);
  if (mipmaps.length === 1) tex.minFilter = THREE.LinearFilter;
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = effectiveSRGB ? THREE.SRGBColorSpace : THREE.NoColorSpace;
  tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  tex.flipY = false;
  tex.needsUpdate = true;
  return tex;
}

/**
 * Fetch + parse multiple DDS files (one per mip level) and assemble them
 * into a single `CompressedTexture` with a full mip chain.
 *
 * Inputs are level-ordered URLs (highest-res first). For one-URL inputs
 * the function reduces to {@link loadDdsTexture} semantics. After
 * concatenation, the mip array is validated for contiguity (each mip is
 * exactly half the previous level in both dimensions); gaps cause
 * truncation since WebGL refuses non-contiguous compressed chains.
 *
 * Returns `null` when every URL fails (404, unsupported format, parse error).
 */
export async function loadDdsMipChain(
  urls: string[],
  sRGB: boolean,
  ddsLoader: DDSLoader,
  renderer: THREE.WebGLRenderer,
): Promise<THREE.Texture | null> {
  if (urls.length === 0) return null;

  // BC4 (RGTC) early-return: software-decode the top mip and return a
  // DataTexture. Bypasses three.js r0.165's broken `RED_RGTC1_Format`
  // upload path. Only the first URL with rgtc kind is used; BC4 nbmask
  // payloads are typically a single .dd0 or .dds anyway.
  for (const url of urls) {
    const probe = await fetchDdsIfSupported(url);
    if (probe?.kind === 'rgtc') {
      const parsed = parseRgtcDds(probe.buf);
      if (!parsed) {
        console.warn(`[dds] BC4 parse failed for ${url}`);
        return null;
      }
      return makeRgtcDataTexture(parsed.mipmaps[0], renderer);
    }
    break;
  }

  type Mip = { data: Uint8Array; width: number; height: number };
  const mips: Mip[] = [];
  let format: THREE.CompressedPixelFormat | null = null;
  let effectiveSRGB = sRGB;

  for (const url of urls) {
    const result = await fetchDdsIfSupported(url);
    if (!result) continue;
    if (result.kind === 'bptc') {
      if (!ensureBptcSupport(renderer)) continue;
      const parsed = parseBptcDds(result.buf);
      if (!parsed) {
        console.warn(`[dds] BC7 parse failed for ${url}`);
        continue;
      }
      if (format === null) format = parsed.format;
      if (parsed.sRGBOverride !== undefined) effectiveSRGB = parsed.sRGBOverride;
      for (const m of parsed.mipmaps) mips.push(m);
    } else {
      let parsed: ReturnType<typeof ddsLoader.parse>;
      try {
        parsed = ddsLoader.parse(result.buf, true);
      } catch (err) {
        console.warn(`[dds] parse failed for ${url}:`, err);
        continue;
      }
      if (!parsed?.mipmaps?.length) continue;
      if (format === null) format = parsed.format as THREE.CompressedPixelFormat;
      for (const m of parsed.mipmaps) mips.push(m as unknown as Mip);
    }
  }

  if (mips.length === 0 || format === null) return null;

  // Validate contiguity. Drop the chain at the first non-half mip.
  const valid: Mip[] = [mips[0]];
  for (let i = 1; i < mips.length; i++) {
    const prev = valid[valid.length - 1];
    const wantW = Math.max(1, prev.width >> 1);
    const wantH = Math.max(1, prev.height >> 1);
    if (mips[i].width !== wantW || mips[i].height !== wantH) {
      console.warn(
        `[dds] mip-chain gap at level ${i}: ` +
          `expected ${wantW}×${wantH}, got ${mips[i].width}×${mips[i].height} — truncating`,
      );
      break;
    }
    valid.push(mips[i]);
  }

  const top = valid[0];
  const tex = new THREE.CompressedTexture(
    valid as unknown as ImageData[],
    top.width,
    top.height,
    format,
  );
  tex.mipmaps = valid as unknown as ImageData[];
  if (valid.length === 1) tex.minFilter = THREE.LinearFilter;
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = effectiveSRGB ? THREE.SRGBColorSpace : THREE.NoColorSpace;
  tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  tex.flipY = false;
  tex.needsUpdate = true;
  return tex;
}

/** PBR slot → glTF-spec sRGB flag. */
export const DDS_SLOT_SRGB: Record<string, boolean> = {
  baseColor: true,
  metallicRoughness: false,
  normal: false,
  occlusion: false,
  emissive: true,
};

/**
 * Cheap-as-possible "what colour is this DDS?" probe. Decodes only the
 * first BC1 colour block of the file (BC1 directly, BC3 = BC1 colour
 * block at +8) and returns the average of its two RGB565 endpoints as
 * a CSS `rgb(r, g, b)` string. Used to swatch mat_camo atlas tiles in
 * the camos panel.
 */
export async function sampleDxtFirstBlockColor(url: string): Promise<string | null> {
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    const buf = new Uint8Array(await resp.arrayBuffer());
    if (buf.length < 132) return null;
    const fourCC = String.fromCharCode(buf[84], buf[85], buf[86], buf[87]);
    let pixelStart = 128;
    let colorOffsetInBlock = 0;
    if (fourCC === 'DXT1') {
      colorOffsetInBlock = 0;
    } else if (fourCC === 'DXT3' || fourCC === 'DXT5') {
      colorOffsetInBlock = 8;
    } else if (fourCC === 'DX10') {
      pixelStart = 148;
      const dxgi = buf[128] | (buf[129] << 8);
      if (dxgi === 71 || dxgi === 72) colorOffsetInBlock = 0;
      else if (dxgi === 74 || dxgi === 77 || dxgi === 78) colorOffsetInBlock = 8;
      else return null;
    } else {
      return null;
    }
    const off = pixelStart + colorOffsetInBlock;
    if (buf.length < off + 4) return null;
    const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const c0 = view.getUint16(off, true);
    const c1 = view.getUint16(off + 2, true);
    const r = (((((c0 >> 11) & 0x1f) + ((c1 >> 11) & 0x1f)) * 255) / 62) | 0;
    const g = (((((c0 >> 5) & 0x3f) + ((c1 >> 5) & 0x3f)) * 255) / 126) | 0;
    const b = ((((c0 & 0x1f) + (c1 & 0x1f)) * 255) / 62) | 0;
    return `rgb(${r}, ${g}, ${b})`;
  } catch {
    return null;
  }
}

export { DDSLoader };
