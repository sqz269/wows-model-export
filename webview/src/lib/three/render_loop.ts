// requestAnimationFrame-driven render loop. Caller passes a `tick`
// function (typically: `controls.update(); renderer.render(scene, camera)`)
// and gets a disposer that cancels the rAF.
//
// Kept tiny so callers compose it directly — a bloom-enabled viewer
// passes `composer.render()` instead, no abstraction needed.

export interface RenderLoopOptions {
  maxFps?: number;
}

export function startRenderLoop(tick: () => void, opts: RenderLoopOptions = {}): () => void {
  const minFrameMs = opts.maxFps && opts.maxFps > 0 ? 1000 / opts.maxFps : 0;
  let lastTick: number | null = null;
  let rafId = requestAnimationFrame(function loop(now) {
    if (minFrameMs <= 0) {
      tick();
    } else if (lastTick == null) {
      lastTick = now;
      tick();
    } else {
      const elapsed = now - lastTick;
      if (elapsed >= minFrameMs) {
        // Carry fractional frame time forward instead of snapping to `now`.
        // On a 60 Hz display, exact rAF deltas often alternate around
        // 16.6/16.7 ms; resetting to now makes a 30 FPS cap miss the
        // 33.333 ms boundary and slip to every third rAF.
        lastTick = now - (elapsed % minFrameMs);
        tick();
      }
    }
    rafId = requestAnimationFrame(loop);
  });
  return () => cancelAnimationFrame(rafId);
}
