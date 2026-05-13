<script lang="ts">
  // Extract page — Vehicle picker driving wows-ingest-ship +
  // wows-ingest-skin-pack. Stub for now; will lift the current
  // tools/webview/src/extract.ts behavior once the backend exposes the
  // job-runner endpoints.
  let { param: _param }: { param: string | null } = $props();
</script>

<section class="page placeholder">
  <h2>Extract</h2>
  <p>
    The Vehicle / permoflage picker is not yet ported. The pipeline still works from the CLI:
    <code>wows-ingest-ship &lt;Ship&gt;</code> and
    <code>wows-ingest-skin-pack &lt;path&gt;</code>.
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
