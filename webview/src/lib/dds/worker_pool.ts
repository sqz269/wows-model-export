// Worker pool dispatcher for DDS parses. Owns a small fleet of dedicated
// Web Workers and routes parse requests round-robin so two slow BC4
// decodes can run in parallel without serializing on one worker. Each
// request gets a monotonic id; responses correlate back to the pending
// resolver via the id. The shared pool (lazily constructed via
// `getSharedPool`) is good enough for every consumer in the texture
// pipeline — there's no benefit to per-cache pools.

import type { DecodeRequest, DecodeResponse, ParseSuccess } from './dds_worker';

interface Pending {
  resolve: (r: ParseSuccess | null) => void;
}

export class DdsWorkerPool {
  private workers: Worker[];
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private rrCursor = 0;

  constructor(size = 2) {
    this.workers = [];
    for (let i = 0; i < size; i++) {
      const w = new Worker(new URL('./dds_worker.ts', import.meta.url), { type: 'module' });
      w.onmessage = (e: MessageEvent<DecodeResponse>) => this.onMessage(e.data);
      w.onerror = (e) => console.error('[dds-worker] error:', e.message || e);
      this.workers.push(w);
    }
  }

  private onMessage(resp: DecodeResponse): void {
    const p = this.pending.get(resp.id);
    if (!p) return;
    this.pending.delete(resp.id);
    if (resp.ok) {
      p.resolve(resp.result);
    } else {
      console.warn('[dds-worker] parse failed:', resp.error);
      p.resolve(null);
    }
  }

  parse(buffer: ArrayBuffer): Promise<ParseSuccess | null> {
    return new Promise((resolve) => {
      const id = this.nextId++;
      this.pending.set(id, { resolve });
      const worker = this.workers[this.rrCursor];
      this.rrCursor = (this.rrCursor + 1) % this.workers.length;
      const req: DecodeRequest = { id, buffer };
      worker.postMessage(req, [buffer]);
    });
  }
}

let shared: DdsWorkerPool | null = null;

/**
 * Lazy singleton. Size capped at 2 — DDS parse is mostly fast (just
 * byte slicing for BC7/DXT) and the heavy case (BC4 software decode) is
 * rare enough that a third worker buys nothing. Workers spawn on first
 * texture toggle so the page load isn't taxed.
 */
export function getSharedPool(): DdsWorkerPool {
  if (!shared) shared = new DdsWorkerPool(2);
  return shared;
}
