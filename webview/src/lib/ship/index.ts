// Public barrel for the ship-viewer subsystem. Pages import from this;
// internal modules import each other directly.

export { ShipViewer } from './viewer';
export type { ShipLoadStats, PickedAssetInfo, PickResult } from './viewer';
export type { ColorMode } from './color_mode';
export type { LodPolicy } from './placement';
export {
  HULL_HIDDEN_GROUPS,
  defaultSeamStates,
  resolveMeshVisibility,
  sectionOfHullMesh,
  shortMeshName,
} from './visibility';
export * as camo from './camo';
export * as textures from './textures';
