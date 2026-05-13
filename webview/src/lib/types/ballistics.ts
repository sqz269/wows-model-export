// Ballistics subset of `<Ship>.meta.json`. Full schema in
// `docs/contracts/sidecar-schema.md` (TODO); only the fields surfaced
// in the Ballistics tab live here.

export type ShellAmmoType = 'AP' | 'HE' | 'CS' | 'torpedo' | string;

export interface ShellEntry {
  ammo_type: ShellAmmoType;
  caliber_mm: number | null;
  mass_kg: number | null;
  muzzle_velocity_mps: number | null;
  air_drag_coefficient: number | null;
  krupp: number | null;
  cap?: boolean | null;
  cap_normalize_max_deg?: number | null;
  fuze_arming_threshold_mm: number | null;
  fuze_delay_s: number | null;
  ricochet_min_deg: number | null;
  ricochet_always_deg: number | null;
  alpha_damage: number | null;
  alpha_piercing_he_mm: number | null;
  alpha_piercing_cs_mm: number | null;
  burn_probability: number | null;
  max_range_m?: number | null;
}

export interface BallisticsSection {
  source?: { generated_at?: string; toolkit_version?: string };
  ranges?: {
    main_battery_m?: number | null;
    secondary_battery_m?: number | null;
    torpedo_max_m?: number | null;
    detection_km?: number | null;
    air_detection_km?: number | null;
  };
  shells?: Record<string, ShellEntry>;
}

// Per-mount ammo summary built from the sidecar's gun-mount sections.
export interface MountAmmoEntry {
  hp_name: string;
  asset_id: string;
  ammo_ids: string[];
}
