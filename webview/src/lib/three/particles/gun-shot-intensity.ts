/** ClientShooterEffects, build 12506899. Barrel diameter is supplied in mm.
 *  The reachable EffectsConstants bytecode loads MAX_NUMBER_INTENSITY_MODIFIER
 *  as 2.5; the old decompiler's -30 literal is unreachable obfuscation.
 *  Keep this aligned with resolve/gameparams_autofill.py:gun_shot_intensity. */
export function gunShotIntensity(mm: number, barrels = 1, modifier = 1): number {
  if (![mm, barrels, modifier].every(Number.isFinite)) return 0;
  const caliber = ((mm / 1000 - 0.05) / 0.5) * 10;
  const barrelFactor = Math.min(2.5, Math.max(1, barrels * 0.6));
  return Math.min(10, Math.max(0, caliber * barrelFactor * modifier));
}
