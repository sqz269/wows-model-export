// `/api/library` client: accessory library index.

import { fetchJson } from './client';
import type { LibraryIndex } from '$lib/types';

export async function fetchLibrary(): Promise<LibraryIndex> {
  return fetchJson<LibraryIndex>('/api/library');
}
