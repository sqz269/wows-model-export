# Exterior-specific ship effects

## Native evidence

Fresh Python decompilation of the retained native client establishes two
independent selection paths. Artifacts are retained locally under
`tmp/pfx_re/exterior_*.py`; current data was checked in the default region of
`J:/ExtractedModels/.cache/gameparams.json`.

- `ShipHull.Hull.__generateExterior`: when the selected skin differs from the
  vehicle and its peculiarity is enabled, select
  `Exterior.effects.get(activeHullKey, baseHull.effects)`. This replaces the
  **whole dictionary**, not individual groups. A present empty dictionary
  suppresses all hull groups; an absent hull key inherits the base dictionary.
- `Peculiarities.getEffect` / `getEffects`: one `peculiarityEffects` lookup per
  original path, gated by the exterior's enabled peculiarity. Substitutions do
  not chain. An `Empty_FX.xml` target is a no-op, not a fallback request.
- `ClientShooterEffects` applies the lookup to shot, lens, purging and
  reload-boost effects. `ClientShotsEffects` applies it to its projectile
  tracer/impact slots. Gun broken/damaged paths and AA aura effects are direct.
- `AttachedEffectsGroup` uses the selected hull table's paths directly. It
  resolves each named node against the loaded model and skips missing nodes.
  Multiple effects on one node must survive; hull paths are not passed through
  the weapon substitution map.

Concrete current examples:

| Exterior | Difference from base |
| --- | --- |
| `PAES488_Azur_Baltimore` | Both funnels use `RunSmoke_big` instead of `RunSmoke_medium`; no weapon substitutions. |
| `PAES458_SPACE_BALTIMORE` | Space smoke/fire plus Space GK/PMK weapon substitutions. |
| `PAES494_Star_Trek_Salem` | Star Trek smoke/shot effects; several purge/reload paths map to `Empty_FX`. |

## Producer and webview contract

`exteriors[].effects.attachments` is a complete, resolved list for the selected
exterior, using the same attachment shape as the base `effects.attachments`.
An absent exterior payload inherits base data; an explicit empty list is valid
and must not fall back. The producer models enabled themed content, consistent
with the webview's existing exterior display; an in-game setting that disables
that theme will intentionally show different effects.

Hull replacements retain the original event names and all authored attachment
rows. Non-hull substitutions preserve anchor and intensity metadata. Variant
hull EP transforms must come from the variant model's effect-point cache,
not from matching names on the base hull. Unresolved attachments remain visible
in the diagnostic counts and are not spawned at an invented position.

Webview rebuilds the particle layer after exterior changes, invalidates pending
record fetches, and releases references to removed mount objects. The VFX node
overlay uses the same selected list. Layer visibility, event-loop choices and
the water toggle survive both mount-only switches and full hull reloads.
Selecting a linked skin follows its exterior, including effects-only exteriors.

## Validation

`npm run test:particles` includes consumer selection/lifecycle regressions:
legacy fallback, empty lists, switch/revert, old mount disposal, fetch
invalidation, hidden-layer switching and control restoration on hull reload.
Native replacement/substitution rules have separate producer tests.

Live validation on 2026-09-08:

- Refreshed all 17 indexed ship sidecars: 45 exterior effect payloads, 38
  variant effect-point harvests, no unresolved hull attachment positions or
  harvest failures. Base effects and unrelated metadata matched their backups.
  Local report: `tmp/pfx_re/fleet_exterior_effects_refresh_report.json`.
- Browser standard -> Azur -> Space -> standard: selected the expected smoke
  and shot paths, restored the exact original list and 198 expanded handles,
  and retained event-loop/water settings. One particle scene root remained.
- Azur: 193 expanded handles, 37/37 renderable effect records loaded; Space:
  182 handles, 34/34 records. Both had zero unresolved renderable anchors.
  The 93 unanchored rows are the separate AA/projectile scope already present
  in the base ship; they are not missing hull effect points.
- Inspected Azur smoke in the live browser and verified the normal exterior
  picker and Particles table use the themed definitions. DDS decode warnings
  were also present during ship texture loading; this work does not resolve
  those material warnings.
- `pytest tests/test_exterior_effects.py tests/test_exterior_unify.py`: 21
  passed (the legacy optional on-disk fixture reports a return-value warning).
  Webview particle suite, Svelte check and production build passed. Build
  retains its large-chunk advisory.

Native AL Aegir, Kearsarge, Massachusetts and Hindenburg already had matching
raw Vehicle effect definitions. Their native exterior routing is now explicit
and handles future overrides; this refresh does not imply a visual change on
those four. The normal variant scaffold uses the already-loaded hull's EP
data, avoiding a second geometry swap for these ships.

These checks establish routing and lifecycle behavior. They do not establish
matched in-game visual parity for every themed effect or add simulation of
unanchored projectile impacts to the ship preview. Unity consumption is a
separate follow-up.
