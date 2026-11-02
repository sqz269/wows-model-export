// Source of the particle simulator's random draws (spawn positions and
// velocities, value-generator picks, atlas cells, spin seeds).
//
// Defaults to Math.random. A ParticleScene that has been given a seed swaps in
// its own generator for the duration of its builds and simulation steps, so
// restarting the effect and replaying the same fixed steps reproduces the same
// particles. That is what lets the inspector rewind to an earlier time.

let current: () => number = Math.random;

/** Uniform random number in [0, 1) from the active source. */
export function particleRandom(): number {
  return current();
}

/** Make `source` the active generator (`null` = Math.random) and return the
 *  previous one, so callers can restore it when their steps finish. */
export function useParticleRandom(source: (() => number) | null): () => number {
  const previous = current;
  current = source ?? Math.random;
  return previous;
}

/** Small, fast seeded generator (mulberry32). Same seed, same sequence; the
 *  whole generator state is `state`, so it can be saved and restored. */
export class SeededParticleRandom {
  state: number;

  constructor(seed: number) {
    this.state = seed >>> 0;
  }

  readonly next = (): number => {
    this.state = (this.state + 0x6d2b79f5) >>> 0;
    let t = this.state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
