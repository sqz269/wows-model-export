<script lang="ts">
  // Extract page — Vehicle picker driving wows-ingest-ship +
  // wows-ingest-skin-pack. Stub for now; will lift the current
  // tools/webview/src/extract.ts behavior once the backend exposes the
  // job-runner endpoints.
  let { param: _param, active: _active }: { param: string | null; active: boolean } = $props();
</script>

<section class="flex-1 overflow-auto p-6">
  <h2 class="text-foreground mb-3 text-lg">Extract</h2>
  <p class="text-muted-foreground max-w-[60ch]">
    The Vehicle / permoflage picker is not yet ported. The pipeline still works from the CLI:
    <code>wows-ingest-ship &lt;Ship&gt;</code> and
    <code>wows-ingest-skin-pack &lt;path&gt;</code>.
  </p>
</section>
