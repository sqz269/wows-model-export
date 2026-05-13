// Thin fetch wrapper that returns typed JSON or throws a structured error.
//
// The dev backend never gets called in production builds (the webview is
// meant to talk to a real FastAPI backend eventually — see
// `migration/PIPELINE_API.md` Option B). Keep the client interface stable
// across the move.

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      try {
        body = await res.text();
      } catch {
        // ignore — body unreadable, fall back to status alone
      }
    }
    throw new ApiError(res.status, body);
  }
  return (await res.json()) as T;
}

/**
 * Resolve a workspace-relative path to a URL the browser can fetch.
 * Used everywhere a sidecar / GLB / DDS / placements JSON URL is needed.
 * The path is URI-encoded so spaces and non-ASCII names (ARP Takao,
 * Yūdachi) round-trip cleanly through the dev backend.
 */
export function repoUrl(relPath: string): string {
  const norm = relPath.replace(/\\/g, '/').replace(/^\/+/, '');
  return '/repo/' + norm.split('/').map(encodeURIComponent).join('/');
}
