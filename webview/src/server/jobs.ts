// In-memory job runner shared by the extract endpoints.
//
// The original webview kept this inline in vite.config.ts; lifting it to
// its own module keeps the endpoint handlers thin and gives a single home
// for the locking + GC rules.
//
// Locking model: each job carries a label (the ship folder name for
// extracts, `<ship>__skin__<id>` for skin packs). `_jobLocks` maps label
// → active jobId; a second job for the same label returns 409 with the
// existing id. Different labels can run concurrently.
//
// GC: jobs are kept in memory for JOB_RETENTION_MS after completion so a
// late poll still picks up the final logs. State is in-process — server
// restart loses all jobs, which is fine for a dev tool.

import { spawn, type ChildProcess } from 'node:child_process';

export type JobState = 'running' | 'done' | 'failed' | 'cancelled';
export type JobKind = 'extract' | 'skin';

export interface Job {
  id: string;
  kind: JobKind;
  label: string;
  state: JobState;
  cmd: string[];
  startedAt: number;
  finishedAt: number | null;
  exitCode: number | null;
  stdout: string;
  stderr: string;
  child: ChildProcess | null;
}

const _jobs: Map<string, Job> = new Map();
const _jobLocks: Map<string, string> = new Map();

const JOB_RETENTION_MS = 60 * 60 * 1000; // 1h after completion
const JOB_MAX_OUTPUT = 1 * 1024 * 1024; // 1 MB per stream — truncate earliest

export class JobLockedError extends Error {
  existingId: string;
  constructor(message: string, existingId: string) {
    super(message);
    this.name = 'JobLockedError';
    this.existingId = existingId;
  }
}

function newJobId(): string {
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

function appendBounded(prev: string, chunk: string): string {
  const combined = prev + chunk;
  if (combined.length <= JOB_MAX_OUTPUT) return combined;
  // Trim from the head, keeping the most recent JOB_MAX_OUTPUT bytes plus
  // a marker so the user sees that earlier output was dropped.
  return '… [earlier output truncated] …\n' + combined.slice(combined.length - JOB_MAX_OUTPUT);
}

export function gcJobs(): void {
  const now = Date.now();
  for (const [id, job] of _jobs) {
    if (job.state === 'running') continue;
    if (job.finishedAt && now - job.finishedAt > JOB_RETENTION_MS) {
      _jobs.delete(id);
      // Drop the lock if it pointed at this job — race-safe via value
      // compare: a newer job for the same label would have overwritten.
      if (_jobLocks.get(job.label) === id) _jobLocks.delete(job.label);
    }
  }
}

export interface SpawnJobOpts {
  kind: JobKind;
  label: string;
  cmd: string[];
  cwd: string;
}

export function spawnJob(opts: SpawnJobOpts): Job {
  gcJobs();
  const existing = _jobLocks.get(opts.label);
  if (existing) {
    const ex = _jobs.get(existing);
    if (ex && ex.state === 'running') {
      throw new JobLockedError(
        `another job for '${opts.label}' is already running (id=${existing})`,
        existing,
      );
    }
  }
  const id = newJobId();
  // PYTHONUNBUFFERED forces line-buffered stdout/stderr in any Python
  // process in the spawn tree (the entry point AND any nested
  // subprocess.run('python ...') calls). Without it, Python detects its
  // stdout is a pipe (not a TTY) and switches to 4 KB block-buffering,
  // so progress prints sit in the buffer for tens of seconds before the
  // client poll sees them.
  const env = { ...process.env, PYTHONUNBUFFERED: '1' };
  const child = spawn(opts.cmd[0], opts.cmd.slice(1), {
    cwd: opts.cwd,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env,
    shell: false,
  });
  const job: Job = {
    id,
    kind: opts.kind,
    label: opts.label,
    state: 'running',
    cmd: opts.cmd,
    startedAt: Date.now(),
    finishedAt: null,
    exitCode: null,
    stdout: '',
    stderr: '',
    child,
  };
  _jobs.set(id, job);
  _jobLocks.set(opts.label, id);

  child.stdout?.setEncoding('utf-8');
  child.stderr?.setEncoding('utf-8');
  child.stdout?.on('data', (chunk: string) => {
    job.stdout = appendBounded(job.stdout, chunk);
  });
  child.stderr?.on('data', (chunk: string) => {
    job.stderr = appendBounded(job.stderr, chunk);
  });
  child.on('error', (err) => {
    job.stderr = appendBounded(job.stderr, `\n[spawn error] ${err.message}\n`);
    job.state = 'failed';
    job.finishedAt = Date.now();
    job.child = null;
    if (_jobLocks.get(opts.label) === id) _jobLocks.delete(opts.label);
  });
  child.on('exit', (code, signal) => {
    if (job.state === 'cancelled') {
      job.exitCode = code;
      return;
    }
    job.exitCode = code;
    if (signal) {
      job.stderr = appendBounded(job.stderr, `\n[exit] signal=${signal}, code=${code}\n`);
    }
    job.state = code === 0 ? 'done' : 'failed';
    job.finishedAt = Date.now();
    job.child = null;
    if (_jobLocks.get(opts.label) === id) _jobLocks.delete(opts.label);
  });
  return job;
}

export function cancelJob(id: string): Job | null {
  const job = _jobs.get(id);
  if (!job) return null;
  if (job.state === 'running' && job.child) {
    try {
      job.child.kill('SIGTERM');
    } catch (err) {
      // SIGTERM may fail on Windows for some processes; log and still
      // mark the job cancelled so the UI unblocks.
      console.warn('[jobs] kill failed:', err);
    }
    job.state = 'cancelled';
    job.finishedAt = Date.now();
    job.stderr = appendBounded(job.stderr, '\n[cancelled by user]\n');
    if (_jobLocks.get(job.label) === job.id) _jobLocks.delete(job.label);
  }
  return job;
}

export function getJob(id: string): Job | null {
  return _jobs.get(id) ?? null;
}

export function listJobs(): Job[] {
  gcJobs();
  return Array.from(_jobs.values()).sort((a, b) => b.startedAt - a.startedAt);
}

export function jobToJson(job: Job): Record<string, unknown> {
  return {
    id: job.id,
    kind: job.kind,
    label: job.label,
    state: job.state,
    cmd: job.cmd,
    started_at: job.startedAt,
    finished_at: job.finishedAt,
    exit_code: job.exitCode,
    stdout: job.stdout,
    stderr: job.stderr,
  };
}

export function readRequestBody(req: {
  on: (ev: string, cb: (chunk: Buffer) => void) => void;
}): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(Buffer.from(c)));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
    req.on('error', (err) => reject(err));
  });
}
