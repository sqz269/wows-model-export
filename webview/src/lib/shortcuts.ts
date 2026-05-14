// Keyboard-shortcut helpers. Centralised so the rule "ignore presses
// while a text input is focused" lives in one place instead of being
// duplicated across page components.

/** Element types where the user is actively typing — shortcuts should
 *  let those keystrokes through to the input instead of firing. */
const TYPING_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

/**
 * Returns true if the keyboard event should be ignored by global
 * shortcuts because the user is mid-typing. Honors:
 *   - editable form fields by tag
 *   - `contenteditable` regions
 *   - `event.defaultPrevented` (something already consumed it)
 */
export function isTypingContext(e: KeyboardEvent): boolean {
  if (e.defaultPrevented) return true;
  const target = e.target;
  if (!(target instanceof HTMLElement)) return false;
  if (TYPING_TAGS.has(target.tagName)) {
    // <input type=button|checkbox|radio|range> isn't typing — only allow
    // typed inputs to swallow keys.
    if (target instanceof HTMLInputElement) {
      const t = target.type;
      if (
        t === 'checkbox' ||
        t === 'radio' ||
        t === 'button' ||
        t === 'submit' ||
        t === 'reset' ||
        t === 'range'
      ) {
        return false;
      }
    }
    return true;
  }
  if (target.isContentEditable) return true;
  return false;
}

/**
 * Returns true if the event carries a non-trivial modifier — used to
 * skip our single-key bindings when the user is doing browser things
 * (Ctrl+R, Cmd+F, etc.) that should fall through to the platform.
 */
export function hasModifier(e: KeyboardEvent): boolean {
  return e.ctrlKey || e.metaKey || e.altKey;
}
