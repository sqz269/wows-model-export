// kind=light components: glow sprites + promoted THREE.PointLights.
// Extracted verbatim from the former monolithic particles.ts.

import * as THREE from 'three';
import type { ParticleComponentBody, ParticleSystemIntensityChannel } from '$lib/types/sidecar';
import {
  NATIVE_TO_METRES,
  PS_IC_LIGHT_RADIUS,
  PS_IC_LIGHT_TINT_B,
  PS_IC_LIGHT_TINT_G,
  PS_IC_LIGHT_TINT_R,
} from './constants';
import { sampleColor, sampleRamp } from './sampling';

let particleLightSpriteTexture: THREE.DataTexture | null = null;

function getParticleLightSpriteTexture(): THREE.DataTexture {
  if (particleLightSpriteTexture) return particleLightSpriteTexture;
  const size = 64;
  const center = (size - 1) * 0.5;
  const invRadius = 1 / center;
  const pixels = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = (x - center) * invRadius;
      const dy = (y - center) * invRadius;
      const r = Math.sqrt(dx * dx + dy * dy);
      const t = Math.max(0, 1 - r);
      const alpha = Math.pow(t, 2.25) * (3 - 2 * t);
      const off = (y * size + x) * 4;
      pixels[off + 0] = 255;
      pixels[off + 1] = 255;
      pixels[off + 2] = 255;
      pixels[off + 3] = Math.max(0, Math.min(255, Math.round(alpha * 255)));
    }
  }
  const tex = new THREE.DataTexture(pixels, size, size, THREE.RGBAFormat, THREE.UnsignedByteType);
  tex.name = 'particle-light-radial-alpha';
  tex.wrapS = THREE.ClampToEdgeWrapping;
  tex.wrapT = THREE.ClampToEdgeWrapping;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.generateMipmaps = false;
  tex.colorSpace = THREE.NoColorSpace;
  tex.needsUpdate = true;
  particleLightSpriteTexture = tex;
  return tex;
}

export class LightRenderer {
  readonly group: THREE.Group;
  readonly sprite: THREE.Sprite;
  /** Index of the system that authored this light's `light` component.
   *  -1 when unknown. Lets the inspector hide a system's lights along with
   *  its sprites when that system is toggled off. */
  ownerSystemIndex = -1;
  pointLight: THREE.PointLight | null = null;
  readonly score: number;
  private elapsed = 0;
  private active = true;
  private readonly material: THREE.SpriteMaterial;
  private intensityValues: number[] = [];
  private lightRadiusMultiplier = 1;
  private lightTintRMultiplier = 1;
  private lightTintGMultiplier = 1;
  private lightTintBMultiplier = 1;

  private static readonly SPRITE_RADIUS_SCALE = 0.25;
  private static readonly SPRITE_MAX_SIZE = 1.25;
  private static readonly SPRITE_OPACITY_SCALE = 0.22;

  constructor(
    private readonly body: ParticleComponentBody,
    private readonly intensityChannels: ParticleSystemIntensityChannel[] = [],
    private readonly intensityDefaults: readonly number[] = [],
  ) {
    this.group = new THREE.Group();
    this.group.name = 'particle-light';
    const pos = body.localPosition;
    if (Array.isArray(pos) && pos.length === 3) {
      // Light offset is a native BW-unit length placed in the ×15 metre frame.
      this.group.position.set(
        pos[0] * NATIVE_TO_METRES,
        pos[1] * NATIVE_TO_METRES,
        pos[2] * NATIVE_TO_METRES,
      );
    }
    this.material = new THREE.SpriteMaterial({
      color: new THREE.Color(1, 1, 1),
      map: getParticleLightSpriteTexture(),
      opacity: 1,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      depthTest: true,
    });
    this.sprite = new THREE.Sprite(this.material);
    this.group.add(this.sprite);
    this.setIntensityValues(intensityDefaults);
    this.score = this.estimateScore();
    this.applySample(0);
  }

  enablePointLight(): void {
    if (this.pointLight) return;
    this.pointLight = new THREE.PointLight(0xffffff, 1, 1, 2);
    this.group.add(this.pointLight);
    this.applySample(this.elapsed);
  }

  setActive(active: boolean): void {
    this.active = active;
    this.group.visible = active;
  }

  restart(): void {
    this.active = true;
    this.group.visible = true;
    this.elapsed = 0;
    this.applySample(0);
  }

  setIntensityValues(values: readonly number[] | undefined): void {
    const count = Math.max(this.intensityChannels.length, this.intensityDefaults.length);
    this.intensityValues = [];
    for (let i = 0; i < count; i++) {
      const authored = values?.[i];
      const fallback = this.intensityDefaults[i] ?? 1;
      this.intensityValues[i] = Number.isFinite(authored) ? Number(authored) : fallback;
    }
    this.applyIntensityState();
    this.applySample(this.elapsed);
  }

  tick(dt: number): void {
    if (!this.active) return;
    this.elapsed += dt;
    this.applySample(this.elapsed);
  }

  dispose(): void {
    this.group.parent?.remove(this.group);
    this.material.dispose();
  }

  private estimateScore(): number {
    const fixed = this.body.color ?? [1, 1, 1, 1];
    let peak = Math.max(
      0,
      fixed[0] * this.lightTintRMultiplier,
      fixed[1] * this.lightTintGMultiplier,
      fixed[2] * this.lightTintBMultiplier,
    );
    for (const p of this.body.colorAnimation?.points ?? []) {
      peak = Math.max(
        peak,
        p.r * this.lightTintRMultiplier,
        p.g * this.lightTintGMultiplier,
        p.b * this.lightTintBMultiplier,
      );
    }
    let radius = Math.max(0, (this.body.radius ?? 0) * this.lightRadiusMultiplier);
    for (const p of this.body.radiusAnimation?.points ?? []) {
      radius = Math.max(radius, p.value * this.lightRadiusMultiplier);
    }
    return peak * Math.max(0.1, radius);
  }

  private applyIntensityState(): void {
    this.lightRadiusMultiplier = 1;
    this.lightTintRMultiplier = 1;
    this.lightTintGMultiplier = 1;
    this.lightTintBMultiplier = 1;
    for (let channelIndex = 0; channelIndex < this.intensityChannels.length; channelIndex++) {
      const channel = this.intensityChannels[channelIndex];
      const value = this.intensityValues[channelIndex] ?? this.intensityDefaults[channelIndex] ?? 1;
      for (const config of channel.configs ?? []) {
        const factor = sampleRamp(config.ramp, value, 1);
        if (!Number.isFinite(factor)) continue;
        for (const flag of config.flags ?? []) {
          switch (flag) {
            case PS_IC_LIGHT_RADIUS:
              this.lightRadiusMultiplier *= factor;
              break;
            case PS_IC_LIGHT_TINT_R:
              this.lightTintRMultiplier *= factor;
              break;
            case PS_IC_LIGHT_TINT_G:
              this.lightTintGMultiplier *= factor;
              break;
            case PS_IC_LIGHT_TINT_B:
              this.lightTintBMultiplier *= factor;
              break;
          }
        }
      }
    }
  }

  private applySample(t: number): void {
    const color = this.sampleColorAt(t);
    // Radius is a native BW-unit influence distance → metres for the ×15 world
    // (drives both the clamped preview flare and the point-light range).
    const radius = Math.max(
      0.01,
      this.sampleRadiusAt(t) * this.lightRadiusMultiplier * NATIVE_TO_METRES,
    );
    const r = Math.max(0, color[0] * this.lightTintRMultiplier);
    const g = Math.max(0, color[1] * this.lightTintGMultiplier);
    const b = Math.max(0, color[2] * this.lightTintBMultiplier);
    const peak = Math.max(r, g, b);
    if (peak > 0) {
      this.material.color.setRGB(r / peak, g / peak, b / peak);
    } else {
      this.material.color.setRGB(0, 0, 0);
    }
    this.material.opacity = Math.max(0, Math.min(1, color[3] * LightRenderer.SPRITE_OPACITY_SCALE));
    // The decoded radius is a point-light influence distance, not the diameter
    // of a visible billboard. Keep the preview flare compact so light metadata
    // does not mask the authored smoke/fire/debris systems.
    const spriteSize = Math.max(
      0.08,
      Math.min(radius * LightRenderer.SPRITE_RADIUS_SCALE, LightRenderer.SPRITE_MAX_SIZE),
    );
    this.sprite.scale.set(spriteSize, spriteSize, spriteSize);
    if (!this.pointLight) return;
    if (peak > 0) {
      this.pointLight.color.setRGB(r / peak, g / peak, b / peak);
      this.pointLight.intensity = peak * Math.max(0, color[3]);
      this.pointLight.distance = radius;
      this.pointLight.visible = this.group.visible;
    } else {
      this.pointLight.intensity = 0;
    }
  }

  private sampleColorAt(t: number): [number, number, number, number] {
    const out = LightRenderer.TMP_COLOR;
    // The decoded period matches the final key time across the current light
    // corpus; there is no repeat flag on the light prototype. Clamp through
    // sampleColor instead of wrapping, otherwise one-shot explosion flashes
    // restart every period and dominate the effect as a recurring orb.
    const axis = t;
    sampleColor(this.body.animatedColor ? this.body.colorAnimation : undefined, axis, out);
    if (!this.body.animatedColor || !this.body.colorAnimation?.points?.length) {
      const fixed = this.body.color ?? [1, 1, 1, 1];
      out[0] = fixed[0];
      out[1] = fixed[1];
      out[2] = fixed[2];
      out[3] = fixed[3];
    }
    return [out[0], out[1], out[2], out[3]];
  }

  private sampleRadiusAt(t: number): number {
    const axis = t;
    return this.body.animatedRadius
      ? sampleRamp(this.body.radiusAnimation, axis, this.body.radius ?? 1)
      : (this.body.radius ?? 1);
  }

  private static readonly TMP_COLOR = new Float32Array(4);
}
