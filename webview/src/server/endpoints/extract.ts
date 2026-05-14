// Extract page endpoints. Picker payload (vehicles + permoflages) and
// the job runner for `wows-ingest-ship` + `wows-ingest-skin-pack`.
//
// Endpoints:
//   GET  /api/gameparams/status     — cache age + size + path
//   GET  /api/extract/snapshot      — full vehicles + permoflages dump
//   POST /api/extract/run           — kick off `wows-ingest-ship`
//   POST /api/extract/skin          — kick off `wows-ingest-skin-pack`
//   GET  /api/extract/jobs          — list all known jobs
//   GET  /api/extract/jobs/:id      — one job (state + accumulated logs)
//   POST /api/extract/jobs/:id/cancel — SIGTERM the child
//
// Snapshot is dumped via the `wows-snapshot` CLI (entry point installed
// from pyproject.toml). The output JSON file lives at
// `<workspace>/.cache/snapshot.json`; we cache the parsed value in memory
// keyed on the joint (gameparams.json mtime, snapshot.json mtime) so a
// refresh of GameParams or a manual re-dump invalidates automatically.

import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';
import type { ViteDevServer } from 'vite';
import {
  JobLockedError,
  cancelJob,
  getJob,
  jobToJson,
  listJobs,
  readRequestBody,
  spawnJob,
} from '../jobs';

const pExec = promisify(execFile);

// ID-shape guards. Each call uses a tight regex so a hostile body can't
// inject extra spawn args; the child runs without a shell so the threat
// surface is small but a small charset keeps the failure modes obvious.
const VEHICLE_ID = /^[A-Za-z0-9_]{3,80}$/;
const LABEL_ID = /^[A-Za-z0-9_\-]{1,80}$/;
const SHIP_FOLDER_ID = /^[A-Za-z0-9_\-]{1,80}$/;
const SKIN_ID = /^[A-Za-z0-9_\-]{1,64}$/;
const JOB_ID = /^[A-Za-z0-9\-]{6,40}$/;

interface ExtractSnapshot {
  vehicles: unknown[];
  permoflages_by_vehicle: Record<string, unknown[]>;
  peculiarity_labels?: Record<string, unknown>;
  summary?: unknown;
}

export function mountExtractApi(server: ViteDevServer, workspace: string): void {
  const cacheDir = path.join(workspace, '.cache');
  const gpCachePath = path.join(cacheDir, 'gameparams.json');
  const snapshotCachePath = path.join(cacheDir, 'snapshot.json');

  // ── Shared snapshot cache ─────────────────────────────────────────────
  // The snapshot dump pays the GameParams parse once per server lifetime
  // (or on cache invalidation). Concurrent first-callers share one
  // in-flight subprocess via _inflight; subsequent callers serve from
  // _cache.data without spawning.
  let _cache: { key: string; data: ExtractSnapshot } | null = null;
  let _inflight: Promise<ExtractSnapshot> | null = null;

  async function ensureSnapshot(): Promise<ExtractSnapshot> {
    if (!fs.existsSync(gpCachePath)) {
      throw new Error(
        `gameparams_cache_missing at ${gpCachePath}. ` +
          'Run `wows-find-ship-variants --refresh` first.',
      );
    }
    const gpMtime = Math.floor(fs.statSync(gpCachePath).mtimeMs);
    const snapMtime = fs.existsSync(snapshotCachePath)
      ? Math.floor(fs.statSync(snapshotCachePath).mtimeMs)
      : 0;
    const key = `${gpMtime}:${snapMtime}`;
    if (_cache && _cache.key === key) return _cache.data;
    if (_inflight) return _inflight;

    _inflight = (async () => {
      try {
        // wows-snapshot writes the JSON file directly (no stdout). Skip
        // the dump when an existing snapshot.json is at least as fresh
        // as gameparams.json — saves the ~30 s parse on cold start when
        // a previous run already wrote one.
        if (!fs.existsSync(snapshotCachePath) || snapMtime < gpMtime) {
          fs.mkdirSync(cacheDir, { recursive: true });
          await pExec('wows-snapshot', ['--output', snapshotCachePath], {
            windowsHide: true,
            cwd: workspace,
            maxBuffer: 64 * 1024 * 1024,
          });
        }
        const text = fs.readFileSync(snapshotCachePath, 'utf-8');
        const data = JSON.parse(text) as ExtractSnapshot;
        const refreshedMtime = Math.floor(fs.statSync(snapshotCachePath).mtimeMs);
        _cache = { key: `${gpMtime}:${refreshedMtime}`, data };
        return data;
      } finally {
        _inflight = null;
      }
    })();
    return _inflight;
  }

  // ── /api/gameparams/status ─────────────────────────────────────────────
  server.middlewares.use('/api/gameparams/status', (_req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-cache');
    try {
      if (!fs.existsSync(gpCachePath)) {
        res.end(
          JSON.stringify({
            exists: false,
            path: gpCachePath,
            hint: 'Run `wows-find-ship-variants --refresh` to populate it.',
          }),
        );
        return;
      }
      const st = fs.statSync(gpCachePath);
      res.end(
        JSON.stringify({
          exists: true,
          path: gpCachePath,
          size_mb: +(st.size / (1024 * 1024)).toFixed(1),
          mtime: Math.floor(st.mtimeMs / 1000),
          mtime_iso: new Date(st.mtimeMs).toISOString(),
        }),
      );
    } catch (err) {
      console.error('[/api/gameparams/status]', err);
      res.statusCode = 500;
      res.end(JSON.stringify({ error: String(err) }));
    }
  });

  // ── /api/extract/snapshot ──────────────────────────────────────────────
  server.middlewares.use('/api/extract/snapshot', async (_req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-cache');
    try {
      const data = await ensureSnapshot();
      res.end(JSON.stringify(data));
    } catch (err) {
      const e = err as { stderr?: string; code?: number; message?: string };
      console.error('[/api/extract/snapshot]', e.message || err);
      const missing = (e.message || '').includes('gameparams_cache_missing');
      res.statusCode = missing ? 503 : 500;
      res.end(
        JSON.stringify({
          ok: false,
          error: e.message || String(err),
          stderr: e.stderr || '',
          code: e.code,
        }),
      );
    }
  });

  // ── /api/extract/run — wows-ingest-ship ────────────────────────────────
  // Body: {
  //   vehicle:     "PASC108" | "PASC108_Baltimore_1944",
  //   label:       "Baltimore",
  //   permoflage:  null | "auto" | "none" | "<exterior_id>",
  //   skip_legacy:    boolean (default true),
  //   build_library: boolean (default false),
  //   and_publish:   boolean (default false),
  //   publish_force: boolean (default false),
  // }
  server.middlewares.use('/api/extract/run', async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.method !== 'POST') {
      res.statusCode = 405;
      res.end(JSON.stringify({ ok: false, error: 'POST required' }));
      return;
    }
    try {
      const body = await readRequestBody(req);
      const parsed = JSON.parse(body || '{}') as {
        vehicle?: string;
        permoflage?: string | null;
        label?: string;
        skip_legacy?: boolean;
        build_library?: boolean;
        and_publish?: boolean;
        publish_force?: boolean;
      };
      const vehicle = String(parsed.vehicle || '');
      const label = String(parsed.label || '');
      if (!VEHICLE_ID.test(vehicle)) {
        res.statusCode = 400;
        res.end(JSON.stringify({ ok: false, error: 'vehicle must be 3-80 chars [A-Za-z0-9_]' }));
        return;
      }
      if (!LABEL_ID.test(label)) {
        res.statusCode = 400;
        res.end(JSON.stringify({ ok: false, error: 'label must be 1-80 chars [A-Za-z0-9_-]' }));
        return;
      }
      let permoflage: string | null = null;
      if (parsed.permoflage === 'none' || parsed.permoflage === 'auto') {
        permoflage = parsed.permoflage;
      } else if (typeof parsed.permoflage === 'string' && parsed.permoflage) {
        if (!VEHICLE_ID.test(parsed.permoflage)) {
          res.statusCode = 400;
          res.end(
            JSON.stringify({
              ok: false,
              error: 'permoflage must be 3-80 chars [A-Za-z0-9_], "auto", "none", or null',
            }),
          );
          return;
        }
        permoflage = parsed.permoflage;
      }

      // Resolve model_dir + the full top_key from the cached snapshot so
      // we can pin --gameparams-ship-id without an extra Python round-trip.
      const snap = await ensureSnapshot();
      const vehicles = snap.vehicles as Array<Record<string, unknown>>;
      const veh = vehicles.find(
        (v) => v.top_key === vehicle || v.param_index === vehicle,
      );
      if (!veh) {
        res.statusCode = 404;
        res.end(JSON.stringify({ ok: false, error: `vehicle ${vehicle} not in GameParams` }));
        return;
      }
      const paramIndex = String(veh.param_index || '');
      const topKeyFull = String(veh.top_key || '') || paramIndex;
      const modelDir = String(veh.model_dir || '') || paramIndex;
      const positional = modelDir || paramIndex;

      // `wows-ingest-ship` accepts a positional ship arg (even when
      // --toolkit-ship overrides it; argparse enforces presence). Pass
      // model_dir so the displayed command matches what the runner spawns.
      const args: string[] = [
        positional,
        '--label',
        label,
        '--toolkit-ship',
        modelDir,
        '--gameparams-ship-id',
        topKeyFull,
        '--non-interactive',
      ];
      if (permoflage !== null) args.push('--variant-permoflage', permoflage);
      if (parsed.skip_legacy !== false) args.push('--skip-legacy');
      if (parsed.build_library) args.push('--build-library');
      if (parsed.and_publish) args.push('--and-publish');
      if (parsed.publish_force) args.push('--publish-force');

      const cmd = ['wows-ingest-ship', ...args];
      try {
        const job = spawnJob({ kind: 'extract', label, cmd, cwd: workspace });
        res.end(JSON.stringify({ ok: true, job_id: job.id, cmd: job.cmd }));
      } catch (err) {
        if (err instanceof JobLockedError) {
          res.statusCode = 409;
          res.end(
            JSON.stringify({
              ok: false,
              error: err.message,
              existing_job_id: err.existingId,
            }),
          );
          return;
        }
        throw err;
      }
    } catch (err) {
      const e = err as { stderr?: string; code?: number; message?: string };
      console.error('[/api/extract/run]', e.message || err);
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: e.message || String(err) }));
    }
  });

  // ── /api/extract/skin — wows-ingest-skin-pack ──────────────────────────
  server.middlewares.use('/api/extract/skin', async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.method !== 'POST') {
      res.statusCode = 405;
      res.end(JSON.stringify({ ok: false, error: 'POST required' }));
      return;
    }
    try {
      const body = await readRequestBody(req);
      const parsed = JSON.parse(body || '{}') as {
        ship?: string;
        source?: string;
        source_arg?: string;
        exterior_id?: string;
        skin_id?: string;
        display_name?: string;
      };
      const ship = String(parsed.ship || '');
      const source = String(parsed.source || '');
      const sourceArg = String(parsed.source_arg || '');
      const skinId = String(parsed.skin_id || '');
      if (!SHIP_FOLDER_ID.test(ship)) {
        res.statusCode = 400;
        res.end(JSON.stringify({ ok: false, error: 'ship must be 1-80 chars [A-Za-z0-9_-]' }));
        return;
      }
      if (source !== 'wg' && source !== 'vfs' && source !== 'loose') {
        res.statusCode = 400;
        res.end(JSON.stringify({ ok: false, error: "source must be 'wg', 'vfs', or 'loose'" }));
        return;
      }
      if (!SKIN_ID.test(skinId)) {
        res.statusCode = 400;
        res.end(JSON.stringify({ ok: false, error: 'skin_id must be 1-64 chars [A-Za-z0-9_-]' }));
        return;
      }
      if (source === 'wg' || source === 'vfs') {
        if (!VEHICLE_ID.test(sourceArg)) {
          res.statusCode = 400;
          res.end(
            JSON.stringify({
              ok: false,
              error: `${source} source_arg must be 3-80 chars [A-Za-z0-9_]`,
            }),
          );
          return;
        }
        if (source === 'vfs' && !parsed.exterior_id) {
          res.statusCode = 400;
          res.end(JSON.stringify({ ok: false, error: 'vfs source requires exterior_id' }));
          return;
        }
        if (parsed.exterior_id && !VEHICLE_ID.test(parsed.exterior_id)) {
          res.statusCode = 400;
          res.end(
            JSON.stringify({ ok: false, error: 'exterior_id must be 3-80 chars [A-Za-z0-9_]' }),
          );
          return;
        }
      } else {
        // loose-mod: server-side absolute path. We only check that the
        // dir exists; the CLI does the real shape validation.
        if (!sourceArg) {
          res.statusCode = 400;
          res.end(
            JSON.stringify({ ok: false, error: 'loose source_arg (folder path) required' }),
          );
          return;
        }
        if (!fs.existsSync(sourceArg) || !fs.statSync(sourceArg).isDirectory()) {
          res.statusCode = 400;
          res.end(
            JSON.stringify({ ok: false, error: `loose source dir not found: ${sourceArg}` }),
          );
          return;
        }
      }
      // Sidecar must exist for the target ship — ingest_skin_pack refuses
      // to run otherwise. A clearer error here beats a spawn-time failure.
      const sidecarPath = path.join(workspace, 'ships', ship, `${ship}.meta.json`);
      if (!fs.existsSync(sidecarPath)) {
        res.statusCode = 404;
        res.end(
          JSON.stringify({
            ok: false,
            error: `sidecar missing: ${sidecarPath}. Run an extract first.`,
          }),
        );
        return;
      }

      const args: string[] = [
        ship,
        '--source',
        `${source}:${sourceArg}`,
        '--skin-id',
        skinId,
      ];
      if (parsed.exterior_id) args.push('--exterior', parsed.exterior_id);
      if (parsed.display_name) args.push('--display-name', parsed.display_name);

      // Different skins for the same ship may run in parallel; the same
      // skin re-trigger is serialised against itself.
      const lockLabel = `${ship}__skin__${skinId}`;
      const cmd = ['wows-ingest-skin-pack', ...args];
      try {
        const job = spawnJob({ kind: 'skin', label: lockLabel, cmd, cwd: workspace });
        res.end(JSON.stringify({ ok: true, job_id: job.id, cmd: job.cmd }));
      } catch (err) {
        if (err instanceof JobLockedError) {
          res.statusCode = 409;
          res.end(
            JSON.stringify({
              ok: false,
              error: err.message,
              existing_job_id: err.existingId,
            }),
          );
          return;
        }
        throw err;
      }
    } catch (err) {
      const e = err as { stderr?: string; code?: number; message?: string };
      console.error('[/api/extract/skin]', e.message || err);
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: e.message || String(err) }));
    }
  });

  // ── /api/extract/jobs[/:id[/cancel]] ───────────────────────────────────
  // GET  /api/extract/jobs        — list all known jobs (newest first)
  // GET  /api/extract/jobs/:id    — one job's state + accumulated logs
  // POST /api/extract/jobs/:id/cancel — SIGTERM the child
  server.middlewares.use('/api/extract/jobs', async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-cache');
    try {
      const url = new URL(req.url || '/', 'http://localhost');
      // The middleware mounts at /api/extract/jobs so url.pathname is the
      // suffix only ('/' or '/<id>' or '/<id>/cancel').
      const tail = (url.pathname || '/').replace(/^\/+/, '');
      if (!tail) {
        const list = listJobs().map((j) => ({
          id: j.id,
          kind: j.kind,
          label: j.label,
          state: j.state,
          started_at: j.startedAt,
          finished_at: j.finishedAt,
          exit_code: j.exitCode,
        }));
        res.end(JSON.stringify({ jobs: list }));
        return;
      }
      const segs = tail.split('/');
      const id = segs[0];
      const action = segs[1] || '';
      if (!JOB_ID.test(id)) {
        res.statusCode = 400;
        res.end(JSON.stringify({ ok: false, error: 'invalid job id' }));
        return;
      }
      const job = getJob(id);
      if (!job) {
        res.statusCode = 404;
        res.end(JSON.stringify({ ok: false, error: 'job not found' }));
        return;
      }
      if (action === 'cancel') {
        if (req.method !== 'POST') {
          res.statusCode = 405;
          res.end(JSON.stringify({ ok: false, error: 'POST required' }));
          return;
        }
        const after = cancelJob(id) ?? job;
        res.end(JSON.stringify({ ok: true, job: jobToJson(after) }));
        return;
      }
      res.end(JSON.stringify({ ok: true, job: jobToJson(job) }));
    } catch (err) {
      console.error('[/api/extract/jobs]', err);
      res.statusCode = 500;
      res.end(JSON.stringify({ ok: false, error: String(err) }));
    }
  });
}
