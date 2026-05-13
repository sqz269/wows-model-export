// Workspace resolution for the Vite dev server.
//
// The webview is a "consumer" of the pipeline's output artifacts: hull GLBs,
// sidecars, accessory library, DDS textures. None of that lives in the
// project repo — it lives in the user's workspace, alongside per-ship
// working dirs (Iowa/, Yamato/, …) and `libraries/accessories/`.
//
// Resolution order:
//   1. $WOWS_WORKSPACE env var (preferred — explicit).
//   2. Walk upwards from the webview dir looking for the
//      `libraries/accessories/` marker (covers the case where the webview
//      is embedded inside a checkout that also contains workspace data —
//      backwards-compat with the legacy `tools/webview/` layout).
//   3. Fall back to the user's home dir (~/wows-workspace).
//
// If the resolved workspace lacks the marker, we log a warning but keep
// going; the dev server starts so the SPA can render its empty state.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const WORKSPACE_MARKER = path.join('libraries', 'accessories');

function hasMarker(p: string): boolean {
  try {
    return fs.existsSync(path.join(p, WORKSPACE_MARKER));
  } catch {
    return false;
  }
}

export function resolveWorkspace(startDir: string): string {
  const override = process.env.WOWS_WORKSPACE;
  if (override) {
    const abs = path.resolve(override);
    if (!hasMarker(abs)) {
      console.warn(
        `[webview] $WOWS_WORKSPACE=${abs} is set but does not contain ` +
          `${WORKSPACE_MARKER}/. Pages that need the accessory library will 404.`,
      );
    }
    return abs;
  }

  // Walk up from the webview dir until we find a workspace marker. Stops at
  // the filesystem root. Handles the common case where someone clones the
  // repo and runs `npm run dev` from inside it without setting env vars.
  let cur = path.resolve(startDir);
  while (true) {
    if (hasMarker(cur)) return cur;
    const parent = path.dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }

  const homeWorkspace = path.join(os.homedir(), 'wows-workspace');
  if (hasMarker(homeWorkspace)) return homeWorkspace;

  console.warn(
    `[webview] No workspace found. Set $WOWS_WORKSPACE or create ` +
      `~/wows-workspace/${WORKSPACE_MARKER}. Falling back to ${homeWorkspace}.`,
  );
  return homeWorkspace;
}

/** Return true when `child` is the same as or a descendant of `parent`. */
export function isChildOf(child: string, parent: string): boolean {
  const rel = path.relative(parent, child);
  return !!rel && !rel.startsWith('..') && !path.isAbsolute(rel);
}
