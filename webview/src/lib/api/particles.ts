// `/api/particles` client helpers shared by particle consumers.

import { fetchJson } from './client';
import type { ParticleRecord } from '$lib/types/sidecar';

interface ParticleRecordResponse {
  ok: boolean;
  record?: ParticleRecord;
  error?: string;
}

export interface ParticleRecordsResult {
  records: Record<string, ParticleRecord>;
  missing: string[];
  errors: Array<{ path: string; error: string }>;
}

export async function fetchParticleRecord(
  path: string,
  quality = 'high',
): Promise<ParticleRecord | null> {
  const qs = new URLSearchParams({ path, quality });
  const res = await fetchJson<ParticleRecordResponse>(`/api/particles/record?${qs}`);
  return res.ok && res.record ? res.record : null;
}

export async function fetchParticleRecords(
  paths: readonly string[],
  opts: { quality?: string; concurrency?: number } = {},
): Promise<ParticleRecordsResult> {
  const quality = opts.quality ?? 'high';
  const unique = [...new Set(paths.filter((p) => p.length > 0))].sort();
  const concurrency = Math.max(1, Math.min(opts.concurrency ?? 8, unique.length || 1));
  const records: Record<string, ParticleRecord> = {};
  const missing: string[] = [];
  const errors: Array<{ path: string; error: string }> = [];
  let cursor = 0;

  const worker = async (): Promise<void> => {
    while (cursor < unique.length) {
      const path = unique[cursor++];
      try {
        const record = await fetchParticleRecord(path, quality);
        if (record) records[path] = record;
        else missing.push(path);
      } catch (err) {
        errors.push({
          path,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
  };

  await Promise.all(Array.from({ length: concurrency }, () => worker()));
  return { records, missing, errors };
}
