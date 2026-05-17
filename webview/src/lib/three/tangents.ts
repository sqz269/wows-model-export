// Compute tangent attribute for a BufferGeometry that lacks it.
//
// WG accessory GLBs ship without the `TANGENT` attribute (verified
// across propellers, barbettes, turrets, etc. — 0/N primitives have it).
// Hull GLBs do ship it (19/19 textured primitives on Montana). Without
// tangents, Three.js's MeshStandardMaterial falls back to
// `perturbNormal2Arb` — a screen-space derivative reconstruction that
// is less stable and visibly weaker than a proper TBN basis.
//
// This helper computes per-vertex averaged tangents via the standard
// Lengyel (2001) algorithm: accumulate triangle-local tangent / bitangent
// contributions per vertex, Gram-Schmidt against the vertex normal,
// store handedness in `.w`. Quality is "good enough" for normal-map
// rendering — not bit-equivalent to MikkTSpace at hard seams but the
// visible difference is minor for our use case (subtle WG normal maps).
//
// Cost: O(triCount) sync work per geometry; called once on first paint.
// `ensureTangents()` is idempotent — bails out immediately if a tangent
// attribute is already present.

import * as THREE from 'three';

/**
 * Compute and attach a `tangent` BufferAttribute (vec4) if missing.
 * Required inputs: `position`, `normal`, `uv`. Indexed geometries
 * supported; non-indexed too.
 *
 * Returns `true` if tangents were freshly computed, `false` if the
 * geometry already had them or lacked required inputs.
 */
export function ensureTangents(geom: THREE.BufferGeometry): boolean {
  if (geom.attributes.tangent) return false;
  const posAttr = geom.attributes.position;
  const normAttr = geom.attributes.normal;
  const uvAttr = geom.attributes.uv;
  if (!posAttr || !normAttr || !uvAttr) return false;

  const pos = posAttr.array as Float32Array;
  const norm = normAttr.array as Float32Array;
  const uv = uvAttr.array as Float32Array;
  const idxArr = geom.index?.array as Uint16Array | Uint32Array | undefined;

  const vertCount = pos.length / 3;
  const tan1 = new Float32Array(vertCount * 3);
  const tan2 = new Float32Array(vertCount * 3);

  const triCount = idxArr ? idxArr.length / 3 : vertCount / 3;

  for (let t = 0; t < triCount; t++) {
    const i0 = idxArr ? idxArr[t * 3] : t * 3;
    const i1 = idxArr ? idxArr[t * 3 + 1] : t * 3 + 1;
    const i2 = idxArr ? idxArr[t * 3 + 2] : t * 3 + 2;

    const p0x = pos[i0 * 3], p0y = pos[i0 * 3 + 1], p0z = pos[i0 * 3 + 2];
    const x1 = pos[i1 * 3] - p0x, y1 = pos[i1 * 3 + 1] - p0y, z1 = pos[i1 * 3 + 2] - p0z;
    const x2 = pos[i2 * 3] - p0x, y2 = pos[i2 * 3 + 1] - p0y, z2 = pos[i2 * 3 + 2] - p0z;

    const u0 = uv[i0 * 2], v0 = uv[i0 * 2 + 1];
    const s1 = uv[i1 * 2] - u0, t1 = uv[i1 * 2 + 1] - v0;
    const s2 = uv[i2 * 2] - u0, t2 = uv[i2 * 2 + 1] - v0;

    const denom = s1 * t2 - s2 * t1;
    if (denom === 0 || !isFinite(denom)) continue;  // degenerate UV triangle
    const r = 1.0 / denom;

    const tx = (t2 * x1 - t1 * x2) * r;
    const ty = (t2 * y1 - t1 * y2) * r;
    const tz = (t2 * z1 - t1 * z2) * r;
    const bx = (s1 * x2 - s2 * x1) * r;
    const by = (s1 * y2 - s2 * y1) * r;
    const bz = (s1 * z2 - s2 * z1) * r;

    tan1[i0 * 3] += tx; tan1[i0 * 3 + 1] += ty; tan1[i0 * 3 + 2] += tz;
    tan1[i1 * 3] += tx; tan1[i1 * 3 + 1] += ty; tan1[i1 * 3 + 2] += tz;
    tan1[i2 * 3] += tx; tan1[i2 * 3 + 1] += ty; tan1[i2 * 3 + 2] += tz;
    tan2[i0 * 3] += bx; tan2[i0 * 3 + 1] += by; tan2[i0 * 3 + 2] += bz;
    tan2[i1 * 3] += bx; tan2[i1 * 3 + 1] += by; tan2[i1 * 3 + 2] += bz;
    tan2[i2 * 3] += bx; tan2[i2 * 3 + 1] += by; tan2[i2 * 3 + 2] += bz;
  }

  const tangents = new Float32Array(vertCount * 4);
  for (let i = 0; i < vertCount; i++) {
    const nx = norm[i * 3], ny = norm[i * 3 + 1], nz = norm[i * 3 + 2];
    let tx = tan1[i * 3], ty = tan1[i * 3 + 1], tz = tan1[i * 3 + 2];
    // Gram-Schmidt: t' = normalize(t - (n·t)·n)
    const ndt = nx * tx + ny * ty + nz * tz;
    tx -= nx * ndt; ty -= ny * ndt; tz -= nz * ndt;
    const len = Math.sqrt(tx * tx + ty * ty + tz * tz);
    if (len > 1e-8) {
      tx /= len; ty /= len; tz /= len;
    } else {
      // Degenerate tangent — fall back to a stable orthogonal axis.
      // Pick the world axis least aligned with the normal.
      const ax = Math.abs(nx), ay = Math.abs(ny);
      if (ax <= ay && ax <= Math.abs(nz)) { tx = 1; ty = 0; tz = 0; }
      else if (ay <= Math.abs(nz))        { tx = 0; ty = 1; tz = 0; }
      else                                  { tx = 0; ty = 0; tz = 1; }
      // Reproject against the normal
      const ndt2 = nx * tx + ny * ty + nz * tz;
      tx -= nx * ndt2; ty -= ny * ndt2; tz -= nz * ndt2;
      const l = Math.sqrt(tx * tx + ty * ty + tz * tz) || 1;
      tx /= l; ty /= l; tz /= l;
    }
    // Handedness: w = sign(dot(cross(n, tan_accumulated), bitan_accumulated))
    // cross(n, t1) — using the *accumulated* tan1, not the orthogonalized.
    const at1x = tan1[i * 3], at1y = tan1[i * 3 + 1], at1z = tan1[i * 3 + 2];
    const at2x = tan2[i * 3], at2y = tan2[i * 3 + 1], at2z = tan2[i * 3 + 2];
    const crx = ny * at1z - nz * at1y;
    const cry = nz * at1x - nx * at1z;
    const crz = nx * at1y - ny * at1x;
    const w = (crx * at2x + cry * at2y + crz * at2z) < 0 ? -1 : 1;

    tangents[i * 4] = tx;
    tangents[i * 4 + 1] = ty;
    tangents[i * 4 + 2] = tz;
    tangents[i * 4 + 3] = w;
  }

  geom.setAttribute('tangent', new THREE.BufferAttribute(tangents, 4));
  return true;
}
