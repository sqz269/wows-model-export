/**
 * Event weapon effects on the ship view: where each laser / wave / missile
 * row is anchored, and the laser beam law. The rules are the client's
 * (build 13533378), documented in
 * reference/engine/wows_event_weapon_effects.md:
 *
 *  - Charge and axis lasers play `gunChargeEffect` on the gun's FIRST
 *    `HP_gunFire` marker (`shootModelNodes[0]`); charge lasers aim a
 *    `laserTargetingEffect` from that marker at the target.
 *  - The beam (`laserMainEffect`) starts at the first marker (charge lasers;
 *    the other kinds start at the server's beam start, which this preview
 *    also puts there), its frame +Z along the beam, channel 0 from the beam
 *    length. The hit at the beam end is the ammo's munition row for the hit
 *    type, frame +Z along the beam.
 *  - Wave emitters play `shotEffect` once at the mean of the gun's markers.
 *  - Missile launchers play `shotEffect` on the launch cell; the preview
 *    plays it on every cell marker, like a gun salvo.
 */
import type { LaserBeamLaw, ParticleAttachment } from '../types/sidecar';

export const LASER_SOURCES: ReadonlySet<string> = new Set([
  'phaserLasers',
  'chargeLasers',
  'axisLaser',
]);

/** Laser groups anchored on the first muzzle marker. */
export const LASER_MUZZLE_GROUPS: ReadonlySet<string> = new Set([
  'gunChargeEffect',
  'laserTargetingEffect',
  'laserMainEffect',
]);

/** Laser groups drawn as a beam from the muzzle to the target. */
export const LASER_BEAM_GROUPS: ReadonlySet<string> = new Set([
  'laserTargetingEffect',
  'laserMainEffect',
]);

/** Gun-like sources whose `shotEffect` plays on every `HP_gunFire` marker. */
const SALVO_SHOT_SOURCES: ReadonlySet<string> = new Set([
  'artillery',
  'atba',
  'airDefense',
  'missiles',
]);

/** Consumer-local group of the preview's beam-end hit (the ammo's munition
 *  row re-anchored at the beam end). Shares the beam's loop key. */
export const LASER_HIT_GROUP = 'laserHit';

/** Horizontal distance from the muzzle to the preview target on the sea, in
 *  ship metres (60 BW, a 1.8 km displayed range). A beam is never drawn
 *  shorter than `min_length_bw` (200 m), so the target sits well past it. */
export const LASER_PREVIEW_RANGE_M = 900;

/** Seconds between the firing preview's shots. Beam records burst once
 *  (`maxEmittingDuration` 0, burst emitters), so the preview replays the beam
 *  and its hit on this clock instead of the generic one-shot loop. */
export const LASER_PREVIEW_FIRE_PERIOD_S = 3;

/** Ship metres per BigWorld unit (BW_TO_SHIP). */
export const SHIP_METRES_PER_BW = 15;

/** The law when a sidecar predates `effects.laser_beam` (client constants). */
export const DEFAULT_LASER_BEAM_LAW: LaserBeamLaw = {
  length_unit: 'bw',
  min_length_bw: 200 / 15,
  max_length_bw: 20000 / 15,
  max_intensity: 10,
  length_channel: 0,
  friendly_channel: 1,
  friendly_intensity: 6,
  enemy_intensity: 0,
  friendly_kinds: ['impulse', 'phaser'],
};

/** `laserLengthToIntensityParam`: beam length in BW -> channel value. */
export function laserLengthToIntensity(lengthBw: number, law: LaserBeamLaw): number {
  const length = Math.min(Math.max(lengthBw, 0), law.max_length_bw);
  if (length <= law.min_length_bw) return 0;
  return (
    ((length - law.min_length_bw) * law.max_intensity) / (law.max_length_bw - law.min_length_bw)
  );
}

/** Channel values for a beam / targeting effect of `lengthBw` at an enemy
 *  target: only the length channel is set (`drawLaserEffect` touches the
 *  friendly channel only for a friendly target); NaN keeps the record's
 *  default on the channels before it. */
export function laserBeamChannels(lengthBw: number, law: LaserBeamLaw): number[] {
  const values = new Array<number>(law.length_channel + 1).fill(NaN);
  values[law.length_channel] = laserLengthToIntensity(lengthBw, law);
  return values;
}

/** How a row is anchored on the mounted asset. */
export type MuzzleAnchorMode = 'all' | 'first' | 'mean' | 'none';

export function muzzleAnchorMode(a: ParticleAttachment): MuzzleAnchorMode {
  const source = a.source ?? 'hull';
  if (LASER_SOURCES.has(source) && LASER_MUZZLE_GROUPS.has(a.group)) return 'first';
  if (a.group !== 'shotEffect') return 'none';
  if (source === 'waves') return 'mean';
  return SALVO_SHOT_SOURCES.has(source) ? 'all' : 'none';
}

/** The munition row a beam ending on the sea plays (`createLaserHitEffect`
 *  with HIT_WATER reads the ammo's `water`), else its `ground`, else the
 *  ship-hit `projDestroyedEffect`. */
export function laserPreviewHitRow(
  beam: ParticleAttachment,
  rows: readonly ParticleAttachment[],
): ParticleAttachment | null {
  if (!beam.ammo_id) return null;
  for (const group of ['water', 'ground', 'projDestroyedEffect']) {
    const row = rows.find(
      (r) => r.source === 'munition' && r.source_id === beam.ammo_id && r.group === group,
    );
    if (row) return row;
  }
  return null;
}
