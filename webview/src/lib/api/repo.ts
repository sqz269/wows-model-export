// Helpers for fetching workspace artifacts through the `/repo/*` endpoint.

import { repoUrl } from './client';
import type { ShipPlacementsDoc, AttachedAccessoriesDoc } from '$lib/types';

export async function fetchPlacements(workspaceRel: string): Promise<ShipPlacementsDoc> {
  const res = await fetch(repoUrl(workspaceRel));
  if (!res.ok) throw new Error(`failed to load placements ${workspaceRel}: HTTP ${res.status}`);
  return (await res.json()) as ShipPlacementsDoc;
}

export async function fetchAttachedAccessories(
  workspaceRel: string,
): Promise<AttachedAccessoriesDoc> {
  const url = repoUrl(workspaceRel) + `?t=${Date.now()}`; // cache-bust during dev
  const res = await fetch(url);
  if (!res.ok) throw new Error(`failed to load attached ${workspaceRel}: HTTP ${res.status}`);
  return (await res.json()) as AttachedAccessoriesDoc;
}

export { repoUrl };
