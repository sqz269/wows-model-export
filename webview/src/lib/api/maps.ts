// Maps API client. Mirrors `extract.ts` shape — thin fetch wrappers
// around `/api/maps/*` returning typed JSON or throwing ApiError.
//
// Phase 1 contract — sync export. Re-evaluate if export latency pushes
// past ~30s (textures-on, large battle maps with eventual forest fix);
// we'd then graduate to the async jobs pattern that `extract.ts` uses.

import { fetchJson } from './client';

export type MapCategory = 'battle' | 'dock' | 'ops' | 'other';

export interface MapExportRecord {
  schema: 'wows_map_export/v1';
  generated_at: string;
  flags: MapExportFlags;
  glb_size: number | null;
  collision_manifest_size?: number | null;
  particle_manifest_size?: number | null;
  particle_manifest?: {
    schema?: string;
    anchor_count?: number;
    resolved_anchor_count?: number;
    unique_resource_path_count?: number;
  } | null;
  particle_manifest_error?: string;
  static_decal_manifest_size?: number | null;
  static_decal_manifest?: {
    schema?: string;
    decal_count?: number;
    valid_transform_count?: number;
    unique_texture_path_count?: number;
    unique_texture_triple_count?: number;
  } | null;
  static_decal_manifest_error?: string;
  probe_manifest_size?: number | null;
  probe_manifest?: {
    schema?: string;
    probe_count?: number;
    valid_transform_count?: number;
    main_probe_count?: number;
    draw_full_scene_count?: number;
    unique_guid_count?: number;
    unique_name_count?: number;
    resolution_counts?: Record<string, number>;
  } | null;
  probe_manifest_error?: string;
  user_object_manifest_size?: number | null;
  user_object_manifest?: {
    schema?: string;
    object_count?: number;
    valid_transform_count?: number;
    well_formed_properties_count?: number;
    visible_model_reference_count?: number;
    waypoint_edge_reference_count?: number;
    unique_type_count?: number;
    type_counts?: Record<string, number>;
  } | null;
  user_object_manifest_error?: string;
  model_instance_manifest_size?: number | null;
  model_instance_manifest?: {
    schema?: string;
    instance_count?: number;
    valid_transform_count?: number;
    landscape_count?: number;
    stable_guid_count?: number;
    dyed_instance_count?: number;
    dye_pair_count?: number;
    unique_dye_pair_count?: number;
    material_override_instance_count?: number;
    material_instance_record_count?: number;
    min_quality_counts?: Record<string, number>;
  } | null;
  model_instance_manifest_error?: string;
  point_light_manifest_size?: number | null;
  point_light_manifest?: {
    schema?: string;
    light_count?: number;
    valid_record_count?: number;
    animated_color_flag_count?: number;
    animated_radius_flag_count?: number;
    color_animation_descriptor_count?: number;
    radius_animation_descriptor_count?: number;
    color_animation_payload_nonzero_count?: number;
    radius_animation_payload_nonzero_count?: number;
    color_animation_oob_count?: number;
    radius_animation_oob_count?: number;
    min_quality_counts?: Record<string, number>;
    radius_min?: number | null;
    radius_max?: number | null;
  } | null;
  point_light_manifest_error?: string;
  elapsed_ms: number;
  stderr: string;
}

export interface MapListEntry {
  name: string;
  vfs_path: string;
  category: MapCategory;
  exported: boolean;
  glb_size?: number;
  collision_manifest_exported: boolean;
  collision_manifest_size?: number;
  particle_manifest_exported: boolean;
  particle_manifest_size?: number;
  static_decal_manifest_exported: boolean;
  static_decal_manifest_size?: number;
  probe_manifest_exported: boolean;
  probe_manifest_size?: number;
  user_object_manifest_exported: boolean;
  user_object_manifest_size?: number;
  model_instance_manifest_exported: boolean;
  model_instance_manifest_size?: number;
  point_light_manifest_exported: boolean;
  point_light_manifest_size?: number;
  export?: MapExportRecord;
}

export interface MapListResponse {
  ok: true;
  items: MapListEntry[];
}

export interface MapExportFlags {
  lod?: number;
  terrain_step?: number;
  no_terrain?: boolean;
  no_water?: boolean;
  no_vegetation?: boolean;
  no_textures?: boolean;
  vegetation_density?: number;
  max_texture_size?: number | null;
  collision_manifest?: boolean;
}

export interface MapExportResponse {
  ok: true;
  name: string;
  glb_path: string;
  glb_size: number | null;
  collision_manifest_path?: string | null;
  collision_manifest_size?: number | null;
  particle_manifest_path?: string | null;
  particle_manifest_size?: number | null;
  particle_manifest?: MapExportRecord['particle_manifest'];
  particle_manifest_error?: string | null;
  static_decal_manifest_path?: string | null;
  static_decal_manifest_size?: number | null;
  static_decal_manifest?: MapExportRecord['static_decal_manifest'];
  static_decal_manifest_error?: string | null;
  probe_manifest_path?: string | null;
  probe_manifest_size?: number | null;
  probe_manifest?: MapExportRecord['probe_manifest'];
  probe_manifest_error?: string | null;
  user_object_manifest_path?: string | null;
  user_object_manifest_size?: number | null;
  user_object_manifest?: MapExportRecord['user_object_manifest'];
  user_object_manifest_error?: string | null;
  model_instance_manifest_path?: string | null;
  model_instance_manifest_size?: number | null;
  model_instance_manifest?: MapExportRecord['model_instance_manifest'];
  model_instance_manifest_error?: string | null;
  point_light_manifest_path?: string | null;
  point_light_manifest_size?: number | null;
  point_light_manifest?: MapExportRecord['point_light_manifest'];
  point_light_manifest_error?: string | null;
  elapsed_ms: number;
  flags: MapExportFlags;
}

export interface MapDeleteResponse {
  ok: true;
  removed: string[];
}

/** List all spaces visible to the toolkit. */
export async function listMaps(): Promise<MapListResponse> {
  return fetchJson<MapListResponse>('/api/maps');
}

/** Trigger `wowsunpack export-map` for one space. Synchronous —
 *  expect 3-8s on default flags; the response carries elapsed_ms. */
export async function exportMap(
  name: string,
  flags: MapExportFlags = {},
): Promise<MapExportResponse> {
  return fetchJson<MapExportResponse>(`/api/maps/${encodeURIComponent(name)}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(flags),
  });
}

/** URL the three.js GLB loader can fetch directly. The backend serves
 *  the file as `model/gltf-binary`. */
export function mapGlbUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/glb`;
}

/** URL for the optional map collision manifest sidecar. */
export function mapCollisionManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/collision-manifest`;
}

/** URL for the map-authored particle anchor manifest sidecar. */
export function mapParticleManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/particle-manifest`;
}

/** URL for the map-authored static decal manifest sidecar. */
export function mapStaticDecalManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/static-decal-manifest`;
}

/** URL for the map-authored probe manifest sidecar. */
export function mapProbeManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/probe-manifest`;
}

/** URL for the map-authored user object manifest sidecar. */
export function mapUserObjectManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/user-object-manifest`;
}

/** URL for map model-instance adjunct metadata. */
export function mapModelInstanceManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/model-instance-manifest`;
}

/** URL for direct `space.bin.pointLights[]` data and animation descriptors. */
export function mapPointLightManifestUrl(name: string): string {
  return `/api/maps/${encodeURIComponent(name)}/point-light-manifest`;
}

/** Drop the cached GLB + export.json. Re-export afterwards to rebuild. */
export async function deleteMapCache(name: string): Promise<MapDeleteResponse> {
  return fetchJson<MapDeleteResponse>(`/api/maps/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
}
