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
  elapsed_ms: number;
  stderr: string;
}

export interface MapListEntry {
  name: string;
  vfs_path: string;
  category: MapCategory;
  exported: boolean;
  glb_size?: number;
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
}

export interface MapExportResponse {
  ok: true;
  name: string;
  glb_path: string;
  glb_size: number | null;
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

/** Drop the cached GLB + export.json. Re-export afterwards to rebuild. */
export async function deleteMapCache(name: string): Promise<MapDeleteResponse> {
  return fetchJson<MapDeleteResponse>(`/api/maps/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
}
