<script lang="ts">
  // Keyboard-shortcut cheat sheet. Opened by the global `?` shortcut in
  // App.svelte (or by clicking the help affordance in the topnav).
  //
  // shadcn-svelte's Dialog already wraps bits-ui with the dev-tool
  // palette + focus trap + ESC/backdrop handling; this file is now
  // just the catalogue and its layout.

  import * as Dialog from '$lib/components/ui/dialog';
  import { Kbd } from '$lib/components/ui/kbd';

  interface Props {
    open: boolean;
    onOpenChange: (v: boolean) => void;
  }

  const { open, onOpenChange }: Props = $props();

  // Single source of truth for the shortcut catalogue. Update here when
  // adding/removing a binding so the help screen stays in sync.
  const SHORTCUTS: Array<{ scope: string; rows: Array<{ keys: string[]; desc: string }> }> = [
    {
      scope: 'Global',
      rows: [
        { keys: ['?'], desc: 'Toggle this help' },
        { keys: ['1'], desc: 'Extract page' },
        { keys: ['2'], desc: 'Ships page' },
        { keys: ['3'], desc: 'Library page' },
        { keys: ['Esc'], desc: 'Close dialogs / cancel' },
      ],
    },
    {
      scope: 'Ships',
      rows: [
        { keys: ['/'], desc: 'Focus ship search' },
        { keys: ['R'], desc: 'Reset camera' },
        { keys: ['F'], desc: 'Frame on selected mesh' },
        { keys: ['click'], desc: 'Inspect mesh under cursor' },
      ],
    },
    {
      scope: 'Library',
      rows: [{ keys: ['/'], desc: 'Focus asset search' }],
    },
  ];
</script>

<Dialog.Root {open} {onOpenChange}>
  <Dialog.Content class="sm:max-w-xl">
    <Dialog.Header>
      <Dialog.Title>Keyboard shortcuts</Dialog.Title>
      <Dialog.Description>
        Hold modifier-free; shortcuts ignore presses inside text inputs.
      </Dialog.Description>
    </Dialog.Header>

    <div class="grid grid-cols-2 gap-x-7 gap-y-4">
      {#each SHORTCUTS as section (section.scope)}
        <section>
          <h3
            class="text-muted-foreground mb-1.5 text-[10px] font-semibold tracking-widest uppercase"
          >
            {section.scope}
          </h3>
          <dl class="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1.5 text-xs">
            {#each section.rows as row (row.desc)}
              <dt class="flex gap-1">
                {#each row.keys as k, i (i)}
                  <Kbd>{k}</Kbd>
                {/each}
              </dt>
              <dd class="text-muted-foreground m-0">{row.desc}</dd>
            {/each}
          </dl>
        </section>
      {/each}
    </div>
  </Dialog.Content>
</Dialog.Root>
