// Three.js particle-system renderer driven by the sidecar's `effects`
// block (Parsed Effect records from `assets.bin`).
//
// MVP scope: CPU-driven point-sprite emitter, one Three.Points instance
// per attachment. We read the parsed authoring data
// (rate ramp / initial position volume / size ramp / tint color curve /
// alpha ramp / force XYZ) and approximate WG's runtime semantics. Not
// bit-exact — see `reference/investigations/particle_work/
// particle_format_spec.md` for the canonical schema. The data here is
// authoritative enough for an inspector / preview rendering.
//
// Performance: each instance maintains a fixed-capacity ring buffer
// (one slot per particle). With <= 200 particles per emitter and <= 50
// active emitters, the JS-side cost stays under 1 ms / frame on a
// recent laptop. Heavier scenes (idle wakes + 4 fires) should switch to
// a GPU-driven backend; that's a future iteration.
//
// Coordinates: most systems (coordinateStyle=2) simulate in the attachment
// group's local frame. WG's detached coordinate styles (0/1/3) sample their
// spawn data in the source attachment frame, then run in the particle-root
// frame so older particles do not keep inheriting future parent motion.
//
// Package layout (split 2026-07-02 from the former single-file particles.ts;
// bodies moved verbatim — see git history of particles.ts for older blame):
//   constants.ts       shared tunables, unit conversions, engine enums
//   sampling.ts        Ramp/Color/ValueGenerator sampling + scalar utils
//   actions.ts         decoded per-component action state
//   velocity-field.ts  velocityField payload fetch + binary decode
//   system-renderer.ts per-system CPU sim + instanced billboard renderer
//   light-renderer.ts  kind=light glow sprites + point lights
//   material.ts        per-system ShaderMaterial builder (blend + GLSL)
//   scene.ts           ParticleScene — the public entry point

export { ParticleScene, type ParticleAttachmentHandle } from './scene';
