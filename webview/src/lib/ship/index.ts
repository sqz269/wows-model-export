// Public barrel for the ship-viewer subsystem. Pages import from this;
// internal modules import each other directly.

export { ShipViewer } from './viewer';
export type {
  ShipLoadStats,
  PickedAssetInfo,
  PickResult,
  ArmorPickResult,
  WgEnvironmentInfo,
} from './viewer';
export { DEFAULT_BLOOM_PARAMS } from '$lib/three/scene';
export type { BloomParams } from '$lib/three/scene';
export type { ColorMode } from './color_mode';
export type { LodPolicy } from './placement';
export {
  HULL_HIDDEN_GROUPS,
  defaultSeamStates,
  resolveMeshVisibility,
  sectionOfHullMesh,
  shortMeshName,
} from './visibility';
export { ARMOR_THICKNESS_STOPS, thicknessToColorHex } from './armor_view';
export { HITBOX_LEGEND, hitboxStyleFor } from './hitbox_view';
export * as camo from './camo';
export * as textures from './textures';
export { TurretRigManager } from './turret_rig';
export type { TurretRig } from './turret_rig';
export {
  NODE_CATEGORIES,
  NODE_CATEGORY_LABEL,
  NODE_CATEGORY_COLOR,
  NODE_CATEGORY_DEFAULT_ON,
} from './node_overlay';
export type { NodeCategory, NodeEntry } from './node_overlay';
