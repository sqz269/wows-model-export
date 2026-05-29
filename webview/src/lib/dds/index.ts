// DDS loading helpers for the library + ship viewers.
//
// Parsing + BC4 software decode run off the main thread via the
// DdsWorkerPool. Main thread only does: fetch (browsers handle this
// off-main already), GL capability checks, and the THREE.Texture
// construction + GPU upload (which has to stay on main because the
// GL context lives there).
//
// Slot/format support:
//   • BC7 (DXGI 98/99)              → CompressedTexture, RGBA_BPTC_Format
//   • BC4 (DXGI 80/81)              → software-decoded to RGBA DataTexture
//     (bypasses three.js r0.165's broken RED_RGTC1 upload path)
//   • DXT1 / DXT3 / DXT5 (classic)  → CompressedTexture, S3TC family
//
// BC5 / BC6H / other DXGI codes are intentionally rejected — none appear
// in WG ship textures and forwarding them as warnings keeps the console
// quiet during normal loads.

import * as THREE from 'three';
import type { ParseSuccess } from './dds_worker';
import { getSharedPool } from './worker_pool';

/** Mip chain entry — a single `.dd0` / `.dd1` / `.dd2` / `.dds` path. */
export type DdsMipPaths = string[];

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

async function fetchBuffer(url: string): Promise<ArrayBuffer | null> {
  const resp = await fetch(url);
  if (!resp.ok) return null;
  return resp.arrayBuffer();
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

function makeRgtcDataTexture(
  mip: { data: Uint8Array; width: number; height: number },
  renderer: THREE.WebGLRenderer,
): THREE.DataTexture {
  // `as unknown as Uint8Array<ArrayBuffer>` strips the SharedArrayBuffer
  // possibility three's DataTexture rejects; the worker allocates plain
  // ArrayBuffer-backed Uint8Arrays so the cast is safe.
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
 * Wrap an uncompressed RGBA8 DDS mip (worker-decoded byte array, already
 * R/G/B/A-ordered with alpha forced to 255 where the source had no
 * alpha channel) into a Three.js ``DataTexture``. Used for
 * GRADIENT_MAP color ramps and any other non-BC particle texture.
 */
function makeRgba8DataTexture(
  mip: { data: Uint8Array; width: number; height: number },
  renderer: THREE.WebGLRenderer,
): THREE.DataTexture {
  const tex = new THREE.DataTexture(
    mip.data as unknown as Uint8Array<ArrayBuffer>,
    mip.width,
    mip.height,
    THREE.RGBAFormat,
    THREE.UnsignedByteType,
  );
  tex.wrapS = THREE.ClampToEdgeWrapping;
  tex.wrapT = THREE.ClampToEdgeWrapping;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  tex.flipY = false;
  tex.colorSpace = THREE.NoColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/**
 * Fetch + parse multiple DDS files (one per mip level) and assemble them
 * into a single `CompressedTexture` with a full mip chain.
 *
 * Inputs are level-ordered URLs (highest-res first). All URLs are fetched
 * in parallel; each ArrayBuffer is then handed to the worker pool for
 * off-main parsing. After concatenation, the mip array is validated for
 * contiguity (each mip is exactly half the previous level in both
 * dimensions); gaps cause truncation since WebGL refuses non-contiguous
 * compressed chains.
 *
 * Returns `null` when every URL fails (404, unsupported format, parse error).
 *
 * Note on UV orientation: Three.js's GLTFLoader does NOT V-flip UVs the
 * way gltFast does, and WebGL ignores `flipY` on compressed textures —
 * UVs passed through unchanged + DDS bytes unflipped makes the raw DDS
 * atlas land right-side-up under a native sample. Do NOT copy the
 * `scale=(1,-1)` V-flip some other consumers apply — see
 * `reference/topics/texture/texture_orientation_investigation.md`.
 */
export async function loadDdsMipChain(
  urls: string[],
  sRGB: boolean,
  renderer: THREE.WebGLRenderer,
): Promise<THREE.Texture | null> {
  if (urls.length === 0) return null;

  // Fetch all mip URLs in parallel. With HTTP/1.1's 6-connection cap the
  // browser still queues at the wire, but we no longer add JS-side
  // serial latency on top of network latency.
  const buffers = await Promise.all(urls.map(fetchBuffer));
  const pool = getSharedPool();
  const parses = await Promise.all(
    buffers.map((buf) => (buf ? pool.parse(buf) : Promise.resolve(null))),
  );

  // RGTC fast-path (BC4 nbmask): worker software-decoded to RGBA. Use
  // the first valid parse and ignore additional level files — BC4 mips
  // are tiny so there's no parallelism win from stitching the chain.
  const firstRgtc = parses.find((p): p is ParseSuccess => !!p && p.kind === 'rgtc');
  if (firstRgtc) {
    if (!ensureRgtcSupport(renderer)) return null;
    return makeRgtcDataTexture(firstRgtc.mipmaps[0], renderer);
  }

  // Uncompressed RGBA8 fast-path. WG ships color ramps (fire / smoke /
  // gradient LUTs) as small uncompressed DDS. No GL extension needed —
  // pump straight into a DataTexture.
  const firstRgba8 = parses.find((p): p is ParseSuccess => !!p && p.kind === 'rgba8');
  if (firstRgba8) {
    return makeRgba8DataTexture(firstRgba8.mipmaps[0], renderer);
  }

  type Mip = ParseSuccess['mipmaps'][number];
  const mips: Mip[] = [];
  let format: number | null = null;
  let kind: ParseSuccess['kind'] | null = null;
  let effectiveSRGB = sRGB;

  for (let i = 0; i < parses.length; i++) {
    const p = parses[i];
    if (!p) continue;
    if (p.kind === 'bptc') {
      if (!ensureBptcSupport(renderer)) continue;
      if (p.sRGBOverride !== undefined) effectiveSRGB = p.sRGBOverride;
    }
    if (kind === null) {
      kind = p.kind;
      format = p.format;
    }
    for (const m of p.mipmaps) mips.push(m);
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
    format as THREE.CompressedPixelFormat,
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
  // Detail atlas: tangent-space normal data in RG, linear/categorical
  // weights in BA. Always sampled linear — the texels are not display-
  // referred colours.
  detail: false,
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
