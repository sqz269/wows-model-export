<script lang="ts">
  // Rig editor: swaps the viewer's loaded GLB for the asset's
  // `.rig.debug.glb` (produced by the rigger's `--debug-scene` path),
  // enables the picker, and lets the user re-classify pieces +
  // designate a face plate. Edits stay in memory until Save.
  //
  // On open:
  //   1. HEAD-probe the debug GLB. 404 → run a rebuild to produce it.
  //   2. Load the debug GLB into the viewer (replaces the regular GLB).
  //   3. Fetch any existing `<asset>.rig_overrides.json` and pre-apply
  //      it as staged state so the user sees their last-saved choices.
  //   4. Enable picker mode.
  //
  // On close: reload the regular GLB. The parent owns the toggle —
  // calling onClose flips it back so AssetDetail clears `rigEditorOpen`.

  import { onMount, untrack } from 'svelte';
  import type { AccessoryViewer } from '$lib/accessory';
  import {
    deleteRigOverrides,
    fetchRigOverrides,
    postRigRebuild,
    repoUrl,
    rigDebugGlbUrl,
    saveRigOverrides,
  } from '$lib/api';
  import type {
    PieceFingerprint,
    PieceInfo,
    RigCategory,
    RigOverridesDoc,
  } from '$lib/types';

  interface Props {
    assetId: string;
    /** GLB-relative path (`asset.glb`). The debug scene + override file
     *  are derived from this. */
    assetGlb: string;
    viewer: AccessoryViewer;
    /** Called when the editor wants to close itself (close button,
     *  fatal load error). Parent flips the toggle back. */
    onClose: () => void;
  }

  const { assetId, assetGlb, viewer, onClose }: Props = $props();

  interface StagedOverride {
    category: RigCategory;
  }

  let pieces = $state<PieceInfo[]>([]);
  // `index → override`. The legacy code used a Map<number, ...>; in
  // Svelte 5 a plain object is easier to make reactive — the keys are
  // small ints and we re-bake the panel on every change anyway.
  let staged = $state<Record<number, StagedOverride>>({});
  // `undefined` = no UI decision yet (use whatever's on disk); `null`
  // = explicit clear; number = staged face-plate piece index.
  let stagedFacePlate = $state<number | null | undefined>(undefined);
  let selectedIdx = $state<number | null>(null);
  let status = $state<{ cls: 'ok' | 'fail' | 'working'; text: string } | null>(null);
  let loaded = $state(false);

  // Mirrors `staged` + `stagedFacePlate` into a single editable
  // payload the Save handler serialises. Keeping it derived means the
  // counter at the top auto-updates.
  const stagedCount = $derived(Object.keys(staged).length);

  const selectedPiece = $derived(selectedIdx === null ? null : pieces[selectedIdx] ?? null);
  const selectedStagedCat = $derived.by((): RigCategory | null => {
    if (selectedIdx === null) return null;
    return staged[selectedIdx]?.category ?? null;
  });
  const selectedIsFacePlate = $derived(
    selectedIdx !== null && stagedFacePlate === selectedIdx,
  );

  /** Per-category piece counts from the rigger's auto-classification.
   *  Renders the "auto: N body / N elev / N skin" summary. */
  const autoCounts = $derived.by(() => {
    const c = { body: 0, elev: 0, skin: 0 };
    for (const p of pieces) c[p.autoCategory] += 1;
    return c;
  });

  function setStatus(cls: 'ok' | 'fail' | 'working', text: string) {
    status = { cls, text };
  }

  /** Re-find a piece across rebuilds by its rigger-baked fingerprint.
   *  Mirrors `RigOverrides`'s server-side matcher: bbox-centre
   *  distance within tolerance, vert-count as primary tiebreaker. */
  function findByFingerprint(fp: PieceFingerprint, tolerance = 0.01): number {
    let bestIdx = -1;
    let bestScore = Infinity;
    for (let i = 0; i < pieces.length; i++) {
      const p = pieces[i];
      const dx = p.fingerprint.center[0] - fp.center[0];
      const dy = p.fingerprint.center[1] - fp.center[1];
      const dz = p.fingerprint.center[2] - fp.center[2];
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 > tolerance * tolerance) continue;
      const dVerts = Math.abs(p.fingerprint.verts - fp.verts);
      const score = dVerts * 1e6 + d2;
      if (score < bestScore) {
        bestScore = score;
        bestIdx = i;
      }
    }
    return bestIdx;
  }

  async function openEditor(): Promise<void> {
    setStatus('working', 'Opening rig editor…');
    const debugUrl = rigDebugGlbUrl(assetGlb);
    // First load attempt — debug.glb may not exist yet (rigger not
    // run with --debug-scene). On 404 we kick off a rebuild and retry.
    let head: Response | null = null;
    try {
      head = await fetch(debugUrl, { method: 'HEAD' });
    } catch {
      // Treat as 404; fall through to the rebuild path.
    }
    if (!head?.ok) {
      setStatus('working', 'No debug scene yet — rebuilding rig…');
      const built = await postRigRebuild(assetId);
      if (!built.ok) {
        setStatus(
          'fail',
          'Rebuild failed — check the dev server logs.\n' +
            (built.stderr || built.error || ''),
        );
        return;
      }
      // Re-probe after the rebuild. The current wows-turret-autorig
      // CLI doesn't emit a debug GLB yet; surface that gap clearly.
      try {
        head = await fetch(rigDebugGlbUrl(assetGlb), { method: 'HEAD' });
      } catch {
        head = null;
      }
      if (!head?.ok) {
        setStatus(
          'fail',
          `Rebuild succeeded but no <${assetId}.rig.debug.glb> was produced — ` +
            '`wows-turret-autorig` does not emit a debug scene yet.',
        );
        return;
      }
    }

    // Existing overrides — load + replay so the user sees their
    // last-saved state on open.
    const existing = await fetchRigOverrides(assetId);

    // Hide pivots while editing — they live in the armature space, not
    // the per-piece debug-scene space.
    viewer.setRigPivotsVisible(false);
    viewer.setRigFlip180(false);

    let result;
    try {
      result = await viewer.loadDebugSceneGlb(rigDebugGlbUrl(assetGlb));
    } catch (err) {
      setStatus('fail', `Load failed: ${err}`);
      return;
    }
    pieces = result.pieces;
    staged = {};
    stagedFacePlate = undefined;
    selectedIdx = null;

    if (existing.ok && existing.exists && existing.doc) {
      const doc = existing.doc;
      for (const e of doc.category_overrides ?? []) {
        const idx = findByFingerprint(e.fingerprint);
        if (idx >= 0) {
          staged[idx] = { category: e.category };
          viewer.setPieceCategoryColor(idx, e.category);
        }
      }
      if (doc.face_plate) {
        const idx = findByFingerprint(doc.face_plate.fingerprint);
        if (idx >= 0) {
          stagedFacePlate = idx;
          viewer.setPieceCategoryColor(idx, 'face');
        }
      }
    }

    viewer.setPickerMode(true);
    viewer.onPiecePicked(handlePiecePicked);
    loaded = true;
    setStatus(
      'ok',
      `Rig editor on — ${pieces.length} pieces. Click pieces to override.`,
    );
  }

  function closeEditor(): void {
    // Tear down picker state; the parent component's load effect
    // will reload the regular GLB next time `url` changes. We trigger
    // the reload by bumping a cache-buster via the parent's onClose
    // → toggle → re-derive of `url`. To be safe, also force a reload
    // of the asset's GLB here so the picker materials definitely go
    // away even if the URL is unchanged.
    viewer.setPickerMode(false);
    viewer.onPiecePicked(null);
    viewer.setSelectedPiece(null);
    // Restore pivot visibility decision to the parent — parent's
    // effect re-pushes `showRigPivots` after the regular GLB loads.
    const regularUrl = repoUrl(`libraries/accessories/${assetGlb}`);
    // Re-load the regular GLB so the picker meshes disappear and the
    // user lands back on the normal asset preview.
    void viewer.loadGlb(regularUrl, null).catch(() => {
      /* tolerated — parent's $effect will re-trigger on close */
    });
    onClose();
  }

  function handlePiecePicked(piece: PieceInfo): void {
    selectedIdx = piece.index;
    viewer.setSelectedPiece(piece.index);
  }

  function setStagedCategory(idx: number, cat: RigCategory): void {
    const piece = pieces[idx];
    if (!piece) return;
    if (cat === piece.autoCategory) {
      // Setting back to auto = remove the staged override.
      delete staged[idx];
      staged = { ...staged };
      viewer.setPieceCategoryColor(idx, 'auto');
    } else {
      staged = { ...staged, [idx]: { category: cat } };
      viewer.setPieceCategoryColor(idx, cat);
    }
    viewer.setSelectedPiece(idx);
  }

  function setStagedFacePlateIdx(idx: number): void {
    // Clear any prior face-plate's visual highlight.
    if (
      typeof stagedFacePlate === 'number' &&
      stagedFacePlate >= 0 &&
      stagedFacePlate !== idx
    ) {
      const prevOv = staged[stagedFacePlate];
      viewer.setPieceCategoryColor(stagedFacePlate, prevOv?.category ?? 'auto');
    }
    stagedFacePlate = idx;
    viewer.setPieceCategoryColor(idx, 'face');
    viewer.setSelectedPiece(idx);
  }

  function clearStagedFacePlate(): void {
    if (typeof stagedFacePlate === 'number' && stagedFacePlate >= 0) {
      const idx = stagedFacePlate;
      const ov = staged[idx];
      viewer.setPieceCategoryColor(idx, ov?.category ?? 'auto');
    }
    stagedFacePlate = null;
  }

  async function save(): Promise<void> {
    const hasCats = Object.keys(staged).length > 0;
    const hasFace = typeof stagedFacePlate === 'number' && stagedFacePlate >= 0;
    if (!hasCats && !hasFace) {
      setStatus('working', 'No overrides — clearing file…');
      const r = await deleteRigOverrides(assetId);
      setStatus(r.ok ? 'ok' : 'fail', r.ok ? 'Overrides cleared.' : `Clear failed: ${r.error}`);
      return;
    }
    const doc: RigOverridesDoc = {
      schema: 'wows_rig_overrides/v1',
      asset_id: assetId,
    };
    if (hasCats) {
      doc.category_overrides = Object.entries(staged).map(([idxStr, ov]) => {
        const idx = Number(idxStr);
        return {
          fingerprint: pieces[idx].fingerprint,
          category: ov.category,
          note: 'edited via webview rig editor',
        };
      });
    }
    if (hasFace) {
      doc.face_plate = {
        fingerprint: pieces[stagedFacePlate as number].fingerprint,
        note: 'edited via webview rig editor',
      };
    }
    setStatus('working', 'Saving…');
    const r = await saveRigOverrides(assetId, doc);
    if (r.ok) {
      const nCats = doc.category_overrides?.length ?? 0;
      setStatus(
        'ok',
        `Saved ${nCats} category override${nCats === 1 ? '' : 's'}` +
          (hasFace ? ' + face-plate' : '') +
          '.',
      );
    } else {
      setStatus('fail', `Save failed: ${r.error}`);
    }
  }

  async function rebuild(): Promise<void> {
    setStatus('working', 'Rebuilding rig (server-side)…');
    const r = await postRigRebuild(assetId);
    if (!r.ok) {
      setStatus('fail', `Rebuild failed: ${r.error || r.stderr || 'unknown'}`);
      return;
    }
    setStatus('working', 'Reloading debug scene…');
    try {
      const result = await viewer.loadDebugSceneGlb(rigDebugGlbUrl(assetGlb));
      pieces = result.pieces;
      staged = {};
      stagedFacePlate = undefined;
      selectedIdx = null;
      viewer.setPickerMode(true);
      viewer.onPiecePicked(handlePiecePicked);

      // Re-apply persisted overrides — the rigger reads them on every
      // run so they survived the rebuild on disk.
      const existing = await fetchRigOverrides(assetId);
      if (existing.ok && existing.exists && existing.doc) {
        for (const e of existing.doc.category_overrides ?? []) {
          const idx = findByFingerprint(e.fingerprint);
          if (idx >= 0) {
            staged[idx] = { category: e.category };
            viewer.setPieceCategoryColor(idx, e.category);
          }
        }
        if (existing.doc.face_plate) {
          const idx = findByFingerprint(existing.doc.face_plate.fingerprint);
          if (idx >= 0) {
            stagedFacePlate = idx;
            viewer.setPieceCategoryColor(idx, 'face');
          }
        }
      }
      setStatus('ok', 'Rebuild complete. Edits reflected in the rig artifacts.');
    } catch (err) {
      setStatus('fail', `Reload failed: ${err}`);
    }
  }

  onMount(() => {
    untrack(() => {
      void openEditor();
    });
    return () => {
      // Best-effort: if the component unmounts mid-edit (user clicks
      // away from the asset), tear down the picker so the next asset's
      // viewer comes up clean.
      viewer.setPickerMode(false);
      viewer.onPiecePicked(null);
      viewer.setSelectedPiece(null);
    };
  });

  const rowCls = 'flex items-center gap-1.5 text-xs';
  const btnCls =
    'rounded border border-border bg-popover px-1.5 py-0.5 text-[11px] hover:bg-accent disabled:opacity-60';
  const btnActiveCls = 'border-primary bg-primary/20';

  function btnFor(cat: RigCategory): string {
    if (!selectedPiece) return btnCls;
    const isActive =
      selectedStagedCat === cat ||
      (selectedStagedCat === null && selectedPiece.autoCategory === cat);
    return isActive ? `${btnCls} ${btnActiveCls}` : btnCls;
  }
</script>

<div class="rounded border border-border bg-popover/40 p-2 text-[11px] flex flex-col gap-2">
  <!-- Legend so users associate colours with categories at a glance. -->
  <div class="flex flex-wrap items-center gap-1.5">
    <span class="rounded px-1 py-[1px]" style="background:#3e2127;color:#ffb3b3">body</span>
    <span class="rounded px-1 py-[1px]" style="background:#1f3a25;color:#8be09a">elev</span>
    <span class="rounded px-1 py-[1px]" style="background:#1c2740;color:#90b3ff">skin</span>
    <span class="rounded px-1 py-[1px]" style="background:#3a2d0f;color:#facc15">face</span>
  </div>

  {#if !loaded && status?.cls === 'working'}
    <div class="text-muted-foreground">{status.text}</div>
  {:else if !loaded && status?.cls === 'fail'}
    <div class="text-destructive whitespace-pre-wrap">{status.text}</div>
  {:else}
    <div class="flex flex-col gap-0.5">
      <div>
        auto:
        <span class="text-rose-300">{autoCounts.body} body</span>
        <span class="text-emerald-300">{autoCounts.elev} elev</span>
        <span class="text-sky-300">{autoCounts.skin} skin</span>
      </div>
      <div>
        staged: {stagedCount} override{stagedCount === 1 ? '' : 's'}{typeof stagedFacePlate ===
          'number' && stagedFacePlate >= 0
          ? ' + face-plate'
          : ''}
      </div>
    </div>

    {#if selectedPiece}
      <div class="flex flex-col gap-1 border-t border-border pt-1.5">
        <div class="text-muted-foreground uppercase tracking-wider">selected</div>
        <div class="break-all font-mono">{selectedPiece.name}</div>
        <div class="text-muted-foreground">
          auto: <strong>{selectedPiece.autoCategory}</strong>
          {selectedPiece.autoFacePlate ? '· auto face-plate' : ''}
          · {selectedPiece.fingerprint.verts} verts
        </div>
        <div class="text-muted-foreground">
          centre [{selectedPiece.fingerprint.center.map((n) => n.toFixed(3)).join(', ')}]
        </div>
        <div class="text-muted-foreground uppercase tracking-wider mt-1">
          override category
        </div>
        <div class={rowCls}>
          <button
            type="button"
            class={btnFor('body')}
            onclick={() => setStagedCategory(selectedPiece.index, 'body')}
          >body</button>
          <button
            type="button"
            class={btnFor('elev')}
            onclick={() => setStagedCategory(selectedPiece.index, 'elev')}
          >elev</button>
          <button
            type="button"
            class={btnFor('skin')}
            onclick={() => setStagedCategory(selectedPiece.index, 'skin')}
          >skin</button>
        </div>
        <div class={rowCls}>
          <button
            type="button"
            class={selectedIsFacePlate ? `${btnCls} ${btnActiveCls}` : btnCls}
            onclick={() => setStagedFacePlateIdx(selectedPiece.index)}
          >
            {selectedIsFacePlate ? '✓ face plate' : 'set as face plate'}
          </button>
          {#if typeof stagedFacePlate === 'number' && stagedFacePlate >= 0}
            <button type="button" class={btnCls} onclick={clearStagedFacePlate}>
              clear face-plate
            </button>
          {/if}
        </div>
      </div>
    {:else}
      <div class="text-muted-foreground">
        Click any piece in the viewer to inspect + override.
      </div>
    {/if}

    <div class="flex flex-wrap items-center gap-1.5 border-t border-border pt-1.5">
      <button type="button" class={btnCls} onclick={save}>Save overrides</button>
      <button type="button" class={btnCls} onclick={rebuild}>Rebuild rig</button>
      <button type="button" class={btnCls} onclick={closeEditor}>Close</button>
    </div>

    {#if status}
      <div
        class="text-[11px] leading-tight"
        class:text-emerald-400={status.cls === 'ok'}
        class:text-destructive={status.cls === 'fail'}
        class:text-muted-foreground={status.cls === 'working'}
      >
        {status.text}
      </div>
    {/if}
  {/if}
</div>
