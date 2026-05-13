// requestAnimationFrame-driven render loop. Caller passes a `tick`
// function (typically: `controls.update(); renderer.render(scene, camera)`)
// and gets a disposer that cancels the rAF.
//
// Kept tiny so callers compose it directly — a bloom-enabled viewer
// passes `composer.render()` instead, no abstraction needed.

export function startRenderLoop(tick: () => void): () => void {
  let rafId = requestAnimationFrame(function loop() {
    tick();
    rafId = requestAnimationFrame(loop);
  });
  return () => cancelAnimationFrame(rafId);
}
