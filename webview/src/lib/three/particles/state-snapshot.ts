// Copy and restore the mutable simulation state of a particle system or light
// renderer, so the inspector can jump back to a checkpoint instead of
// replaying an effect from time 0.
//
// The copy is generic: every own field that is a number, boolean, string,
// typed array, number array or three.js math value, plus the same kinds of
// fields one level inside plain objects and arrays of plain objects (action
// state, clocks). three.js scene objects (meshes, geometry, materials) are
// skipped; they only display the simulation and are rewritten from it.
// Typed arrays are restored in place, because buffer attributes share them.

import * as THREE from 'three';

type MathValue =
  | THREE.Vector2
  | THREE.Vector3
  | THREE.Vector4
  | THREE.Quaternion
  | THREE.Color
  | THREE.Matrix3
  | THREE.Matrix4;

type Leaf = { value: unknown };
type FlatState = Array<[string, Leaf]>;
type FieldState =
  | [key: string, kind: 'leaf', state: Leaf]
  | [key: string, kind: 'object', state: FlatState]
  | [key: string, kind: 'array', state: FlatState[]];

/** Opaque snapshot of one object's simulation state. */
export type ObjectState = FieldState[];

function isMathValue(value: unknown): value is MathValue {
  return (
    value instanceof THREE.Vector2 ||
    value instanceof THREE.Vector3 ||
    value instanceof THREE.Vector4 ||
    value instanceof THREE.Quaternion ||
    value instanceof THREE.Color ||
    value instanceof THREE.Matrix3 ||
    value instanceof THREE.Matrix4
  );
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return (
    value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype
  );
}

function isTypedArray(value: unknown): value is Float32Array | Int32Array | Uint32Array {
  return ArrayBuffer.isView(value) && !(value instanceof DataView);
}

function captureLeaf(value: unknown): Leaf | null {
  const t = typeof value;
  if (
    t === 'number' ||
    t === 'boolean' ||
    t === 'string' ||
    value === undefined ||
    value === null
  ) {
    return { value };
  }
  if (isTypedArray(value)) return { value: value.slice() };
  if (isMathValue(value)) return { value: value.clone() };
  if (Array.isArray(value) && value.every((v) => typeof v === 'number')) {
    return { value: value.slice() };
  }
  return null;
}

function captureFlat(obj: Record<string, unknown>): FlatState {
  const out: FlatState = [];
  for (const key of Object.keys(obj)) {
    const leaf = captureLeaf(obj[key]);
    if (leaf) out.push([key, leaf]);
  }
  return out;
}

/** Restore one field; typed arrays and math values are written in place. */
function restoreLeaf(obj: Record<string, unknown>, key: string, leaf: Leaf): void {
  const saved = leaf.value;
  const current = obj[key];
  if (isTypedArray(saved) && isTypedArray(current) && current.length === saved.length) {
    current.set(saved as never);
  } else if (isTypedArray(saved)) {
    obj[key] = saved.slice();
  } else if (isMathValue(saved) && isMathValue(current)) {
    (current as { copy(v: MathValue): unknown }).copy(saved);
  } else if (Array.isArray(saved)) {
    obj[key] = saved.slice();
  } else {
    obj[key] = saved;
  }
}

function restoreFlat(obj: Record<string, unknown>, state: FlatState): void {
  for (const [key, leaf] of state) restoreLeaf(obj, key, leaf);
}

export function captureObjectState(target: object): ObjectState {
  const obj = target as Record<string, unknown>;
  const out: ObjectState = [];
  for (const key of Object.keys(obj)) {
    const value = obj[key];
    const leaf = captureLeaf(value);
    if (leaf) out.push([key, 'leaf', leaf]);
    else if (isPlainObject(value)) out.push([key, 'object', captureFlat(value)]);
    else if (Array.isArray(value) && value.length > 0 && value.every(isPlainObject)) {
      out.push([key, 'array', value.map((v) => captureFlat(v as Record<string, unknown>))]);
    }
  }
  return out;
}

/** Write a snapshot back. An array of state objects whose length changed
 *  since the capture is left alone: simulation action lists are fixed at
 *  construction, so only display-side pools (run meshes) ever change size. */
export function restoreObjectState(target: object, state: ObjectState): void {
  const obj = target as Record<string, unknown>;
  for (const field of state) {
    const [key, kind] = field;
    if (kind === 'leaf') restoreLeaf(obj, key, field[2]);
    else if (kind === 'object') {
      const current = obj[key];
      if (isPlainObject(current)) restoreFlat(current, field[2]);
    } else {
      const current = obj[key];
      if (!Array.isArray(current) || current.length !== field[2].length) continue;
      field[2].forEach((s, i) => restoreFlat(current[i] as Record<string, unknown>, s));
    }
  }
}

/** Bytes held by a snapshot's typed arrays (for checkpoint budgeting). */
export function objectStateBytes(state: ObjectState): number {
  let bytes = 0;
  const add = (leaf: Leaf) => {
    if (isTypedArray(leaf.value)) bytes += leaf.value.byteLength;
  };
  for (const field of state) {
    if (field[1] === 'leaf') add(field[2]);
    else if (field[1] === 'object') for (const [, leaf] of field[2]) add(leaf);
    else for (const flat of field[2]) for (const [, leaf] of flat) add(leaf);
  }
  return bytes;
}
