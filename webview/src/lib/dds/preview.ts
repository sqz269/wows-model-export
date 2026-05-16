// DDS → Blob URL decoder for static thumbnails.
//
// Reuses the texture-loading pipeline (`loadDdsMipChain`) so we get
// BC7 / DXT / BC4 support without re-implementing the parsers. The
// decoded `THREE.Texture` is rendered to a small off-screen WebGL
// canvas as a full-screen quad, then captured with `canvas.toBlob`
// for use as an `<img src>`. This lets thumbnails live in normal DOM
// flow without each preview owning a WebGL context (Chrome caps the
// per-page count around 16).
//
// One shared renderer + serialized decode queue. Cheap enough — a
// 1024² BC7 thumbnail decodes in ~10 ms on a modern laptop and we
// only ever have a handful on screen.

import * as THREE from 'three';
import { loadDdsMipChain, resolveDdsMipUrls } from '.';

export interface DdsPreviewResult {
  blobUrl: string;
  width: number;
  height: number;
}

const PREVIEW_SIZE = 256;

interface SharedState {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.OrthographicCamera;
  quad: THREE.Mesh;
}

let _shared: SharedState | null = null;

function ensureShared(): SharedState | null {
  if (_shared) return _shared;
  // Some browsers/CI fail to construct WebGL — surface as a null result
  // so the caller renders a fallback chip instead of throwing.
  let renderer: THREE.WebGLRenderer;
  try {
    const canvas = document.createElement('canvas');
    canvas.width = PREVIEW_SIZE;
    canvas.height = PREVIEW_SIZE;
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: false,
      alpha: true,
      // `toBlob` reads from the GL buffer after the next composite; with
      // a default config the buffer is cleared by the time we get here.
      // Preserve so the capture sees the rendered quad.
      preserveDrawingBuffer: true,
      premultipliedAlpha: false,
    });
  } catch (err) {
    console.warn('[dds-preview] could not create WebGLRenderer:', err);
    return null;
  }
  renderer.setSize(PREVIEW_SIZE, PREVIEW_SIZE, false);
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  // Unit-square orthographic — quad spans (-1, 1) × (-1, 1).
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const geom = new THREE.PlaneGeometry(2, 2);
  const mat = new THREE.MeshBasicMaterial({
    map: null,
    transparent: true,
    depthTest: false,
    depthWrite: false,
  });
  const quad = new THREE.Mesh(geom, mat);
  scene.add(quad);
  _shared = { renderer, scene, camera, quad };
  return _shared;
}

// Serialize decodes so concurrent requests can't race on the shared
// canvas. The chain only swallows rejections (so one failure doesn't
// stick the queue) — the rejected promise is still returned to its
// caller via `next`.
let _queue: Promise<unknown> = Promise.resolve();

/**
 * Decode + render a DDS mip-chain to a PNG Blob URL the browser can
 * display in an `<img>`. URLs must be the workspace-resolved form
 * (post-`resolveDdsMipUrls`); pass `[]` for placeholders and the call
 * resolves to `null` without touching the renderer.
 *
 * Caller owns the returned blob URL — revoke via `URL.revokeObjectURL`
 * when the preview unmounts.
 */
export function decodeDdsToBlobUrl(
  urls: string[],
  sRGB: boolean,
): Promise<DdsPreviewResult | null> {
  const next = _queue.then(() => _decode(urls, sRGB));
  // Reset the chain to a resolved-promise sentinel so the next decode
  // doesn't inherit our error / result.
  _queue = next.then(
    () => undefined,
    () => undefined,
  );
  return next;
}

/**
 * Sugar over `decodeDdsToBlobUrl` that takes the asset-level
 * `texture_sets` mip-path array (relative to `libraries/accessories/`)
 * and the page base URL. Returns null for placeholder textures
 * (`default_ao.dds`) and for empty mip arrays.
 */
export function decodeDdsTextureSetSlot(
  mipPaths: string[] | undefined,
  baseUrl: string,
  sRGB: boolean,
): Promise<DdsPreviewResult | null> {
  const urls = resolveDdsMipUrls(mipPaths, baseUrl);
  if (urls.length === 0) return Promise.resolve(null);
  return decodeDdsToBlobUrl(urls, sRGB);
}

async function _decode(
  urls: string[],
  sRGB: boolean,
): Promise<DdsPreviewResult | null> {
  if (urls.length === 0) return null;
  const shared = ensureShared();
  if (!shared) return null;
  const { renderer, scene, camera, quad } = shared;
  const tex = await loadDdsMipChain(urls, sRGB, renderer);
  if (!tex) return null;
  // CompressedTexture has its dimensions on `mipmaps[0]`; DataTexture
  // (BC4 software path) has them on `image`. Cover both.
  const width =
    (tex as { mipmaps?: { width: number }[] }).mipmaps?.[0]?.width ??
    (tex as { image?: { width: number } }).image?.width ??
    PREVIEW_SIZE;
  const height =
    (tex as { mipmaps?: { height: number }[] }).mipmaps?.[0]?.height ??
    (tex as { image?: { height: number } }).image?.height ??
    PREVIEW_SIZE;
  const mat = quad.material as THREE.MeshBasicMaterial;
  try {
    mat.map = tex;
    mat.needsUpdate = true;
    renderer.clear();
    renderer.render(scene, camera);
    const blob = await new Promise<Blob | null>((resolve) =>
      renderer.domElement.toBlob(resolve, 'image/png'),
    );
    if (!blob) return null;
    return {
      blobUrl: URL.createObjectURL(blob),
      width,
      height,
    };
  } finally {
    // Always drop the GPU resources — we don't reuse decoded textures
    // because the next preview will overwrite them anyway, and holding
    // them pins memory unboundedly across a long browsing session.
    mat.map = null;
    tex.dispose();
  }
}
