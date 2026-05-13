<script lang="ts">
  // Library page — accessory browser. Stub for now; will lift the current
  // tools/webview/src/library.ts behavior in a follow-up pass.
  //
  // Accepts `param` (e.g. `asset/<id>`) for parity with the legacy
  // `#/asset/<id>` deep-link, but doesn't read it yet — underscore prefix
  // satisfies our unused-vars convention.
  let { param: _param }: { param: string | null } = $props();
</script>

<section class="page placeholder">
  <h2>Library</h2>
  <p>
    The accessory browser is not yet ported. Run
    <code>wows-build-accessory-library</code> against your workspace; then come back here once the page
    lands.
  </p>
</section>

<style>
  .page {
    flex: 1 1 auto;
    padding: 24px;
    overflow: auto;
  }
  .placeholder h2 {
    margin: 0 0 12px;
    font-size: 18px;
    color: var(--fg);
  }
  .placeholder p {
    color: var(--fg-dim);
    max-width: 60ch;
  }
</style>
