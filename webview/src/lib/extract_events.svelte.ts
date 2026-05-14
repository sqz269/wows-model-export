// Cross-route signal for "an extract/skin-pack job just finished
// successfully". The Extract page's poll loop observes the running →
// done transition and bumps `completionRevision`; the Ships and
// Library routes subscribe via `$effect` and re-fetch their derived
// state (ship summaries, accessory index) so newly-extracted content
// appears without a manual page reload.
//
// In-memory only — a page reload resets the counter to 0, which is
// fine because each route also re-fetches on mount. Failed/cancelled
// jobs do NOT bump the counter (the workspace didn't change).

class ExtractEvents {
  /** Monotonically increasing. Effects subscribe by reading this
   *  inside their tracked block. */
  completionRevision = $state(0);

  /** Label of the most recent successful completion (for toast text). */
  lastCompletedLabel = $state<string | null>(null);

  /** Kind of the most recent successful completion. */
  lastCompletedKind = $state<'extract' | 'skin' | null>(null);
}

export const extractEvents = new ExtractEvents();
