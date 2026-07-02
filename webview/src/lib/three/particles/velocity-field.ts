// velocityField action payload: fetch + binary decode (+half-float).
// Extracted verbatim from the former monolithic particles.ts.

import { repoUrl } from '$lib/api';

export interface VelocityFieldData {
  sizeX: number;
  sizeY: number;
  sizeZ: number;
  vectors: Float32Array;
}

const velocityFieldCache = new Map<string, Promise<VelocityFieldData | null>>();

export function fetchVelocityField(path: string): Promise<VelocityFieldData | null> {
  let pending = velocityFieldCache.get(path);
  if (!pending) {
    pending = fetch(repoUrl(path))
      .then(async (res) => {
        if (!res.ok) return null;
        const field = decodeVelocityField(await res.arrayBuffer());
        if (!field) console.warn('[particles] velocity field decode failed', path);
        return field;
      })
      .catch((err) => {
        console.warn('[particles] velocity field load failed', path, err);
        return null;
      });
    velocityFieldCache.set(path, pending);
  }
  return pending;
}

function decodeVelocityField(buffer: ArrayBuffer): VelocityFieldData | null {
  if (buffer.byteLength < 24) return null;
  const view = new DataView(buffer);
  const production = decodeProductionVelocityField(view, buffer.byteLength);
  if (production) return production;
  return decodeLegacyVelocityField(view, buffer.byteLength);
}

function decodeProductionVelocityField(
  view: DataView,
  byteLength: number,
): VelocityFieldData | null {
  const sizeX = view.getUint32(0, true);
  const sizeY = view.getUint32(4, true);
  const sizeZ = view.getUint32(8, true);
  const scalarCount = view.getUint32(12, true);
  const dataOffset = view.getUint32(16, true) + view.getUint32(20, true) * 0x100000000;
  const expectedCount = sizeX * sizeY * sizeZ * 3;
  if (
    sizeX <= 0 ||
    sizeY <= 0 ||
    sizeZ <= 0 ||
    sizeX > 256 ||
    sizeY > 256 ||
    sizeZ > 256 ||
    scalarCount !== expectedCount ||
    dataOffset < 24 ||
    dataOffset + scalarCount * 2 > byteLength
  ) {
    return null;
  }
  const vectors = new Float32Array(expectedCount);
  for (let i = 0; i < expectedCount; i++) {
    vectors[i] = halfToFloat(view.getUint16(dataOffset + i * 2, true));
  }
  return { sizeX, sizeY, sizeZ, vectors };
}

function decodeLegacyVelocityField(view: DataView, byteLength: number): VelocityFieldData | null {
  if (byteLength < 8 || view.getUint32(0, true) !== 0x444c4656) return null; // "VFLD"
  if (view.getUint8(4) !== 1) return null;
  const sizeX = view.getUint8(5);
  const sizeY = view.getUint8(6);
  const sizeZ = view.getUint8(7);
  const count = sizeX * sizeY * sizeZ * 3;
  if (sizeX <= 0 || sizeY <= 0 || sizeZ <= 0 || byteLength < 8 + count * 2) return null;
  const vectors = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    vectors[i] = Math.max(-1, view.getInt16(8 + i * 2, true) / 32767);
  }
  return { sizeX, sizeY, sizeZ, vectors };
}

function halfToFloat(bits: number): number {
  const sign = bits & 0x8000 ? -1 : 1;
  const exponent = (bits >> 10) & 0x1f;
  const mantissa = bits & 0x03ff;
  if (exponent === 0) {
    return mantissa === 0 ? sign * 0 : sign * Math.pow(2, -14) * (mantissa / 1024);
  }
  if (exponent === 0x1f) {
    return mantissa === 0 ? sign * Infinity : NaN;
  }
  return sign * Math.pow(2, exponent - 15) * (1 + mantissa / 1024);
}
