// `/api/gameparams/*` client. Three endpoints:
//   GET /types                        → typeinfo.type histogram
//   GET /list?type=&q=&limit=&offset= → paginated summary rows
//   GET /entity/{id}                  → full record JSON
//
// First call to /types or /list triggers the backend's 3-4 GB
// flat-load — subsequent calls hit the per-process cache and return
// instantly. /entity also benefits from that cache.
//
// The browser route does no caching of its own; the network is fast
// once GameParams is resident. If that changes (e.g. switching to a
// streaming/partial-load backend) we can add a per-id memoization
// here.

import { fetchJson } from './client';
import type {
  GameParamEntity,
  GameParamListResult,
  GameParamTypeHistogram,
} from '$lib/types';

export function fetchGameParamTypes(): Promise<GameParamTypeHistogram> {
  return fetchJson<GameParamTypeHistogram>('/api/gameparams/types');
}

export interface ListQuery {
  type?: string | null;
  q?: string | null;
  limit?: number;
  offset?: number;
}

export function fetchGameParamList(query: ListQuery = {}): Promise<GameParamListResult> {
  const params = new URLSearchParams();
  if (query.type) params.set('type', query.type);
  if (query.q) params.set('q', query.q);
  if (query.limit != null) params.set('limit', String(query.limit));
  if (query.offset != null) params.set('offset', String(query.offset));
  const qs = params.toString();
  return fetchJson<GameParamListResult>(
    '/api/gameparams/list' + (qs ? `?${qs}` : ''),
  );
}

export function fetchGameParamEntity(entityId: string): Promise<GameParamEntity> {
  return fetchJson<GameParamEntity>(
    `/api/gameparams/entity/${encodeURIComponent(entityId)}`,
  );
}
