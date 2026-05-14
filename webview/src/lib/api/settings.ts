// `/api/settings` client: read + persist the user-config file the
// backend reads at startup.
//
// The page wires GET into a form, lets the user edit, and PUTs back.
// Restart is required for the running backend to pick up the new values
// — the response carries `restart_required: true` for the UI to
// surface.

import { fetchJson } from './client';

/** Where the field's currently-resolved value came from. */
export type SettingSource = 'env' | 'file' | 'auto' | 'default' | 'unconfigured';

export interface SettingsField<T> {
  /** Currently-resolved value, or null when nothing supplied it. */
  value: T | null;
  source: SettingSource;
  /** Env-var name the user can set to override the file. Surfaced as a
   *  hint when `source === 'env'`. */
  env_var: string;
}

export interface SettingsResponse {
  /** Absolute filesystem path the PUT writes to. Shown for transparency. */
  config_path: string;
  /** Snapshot of the workspace the backend BOOTED with. A user-config
   *  PUT changing `fields.workspace` won't move this — surface the gap
   *  to the user as "restart required". */
  running_workspace: string;
  running_cache_dir: string | null;
  fields: {
    game_dir: SettingsField<string>;
    toolkit_bin: SettingsField<string>;
    workspace: SettingsField<string>;
    toolkit_timeout_s: SettingsField<number>;
  };
}

export interface SettingsPatch {
  /** `null` / empty string clears the override; falls back to env or auto. */
  game_dir?: string | null;
  toolkit_bin?: string | null;
  workspace?: string | null;
  toolkit_timeout_s?: number | null;
}

export interface SettingsPutResponse {
  ok: true;
  /** Always true for now — the running backend doesn't hot-swap. */
  restart_required: boolean;
  config_path: string;
  saved: Partial<SettingsPatch>;
}

export function fetchSettings(): Promise<SettingsResponse> {
  return fetchJson<SettingsResponse>('/api/settings');
}

export function saveSettings(patch: SettingsPatch): Promise<SettingsPutResponse> {
  return fetchJson<SettingsPutResponse>('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}
