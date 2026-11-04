// #150 — accent-colour presets.
//
// The colour values themselves live in theme.css as
// `[data-accent="<id>"]` rules (one set per theme), so this module only
// carries ids + labels and the helper that stamps the attribute on
// <html>. Keep the id list in sync with `ACCENT_COLORS` in
// ha-addon/server/settings.py (the server rejects unknown ids).

export const ACCENT_COLORS = [
  { id: 'indigo', label: 'Indigo (default)' },
  { id: 'blue', label: 'Blue' },
  { id: 'teal', label: 'Teal' },
  { id: 'green', label: 'Green' },
  { id: 'amber', label: 'Amber' },
  { id: 'orange', label: 'Orange' },
  { id: 'rose', label: 'Rose' },
  { id: 'violet', label: 'Violet' },
] as const;

export type AccentColor = (typeof ACCENT_COLORS)[number]['id'];

export const DEFAULT_ACCENT: AccentColor = 'indigo';

// localStorage key used to re-apply the last-known accent before the
// settings fetch resolves, so a reload doesn't flash indigo first.
const STORAGE_KEY = 'accent_color';

function isAccent(v: unknown): v is AccentColor {
  return typeof v === 'string' && ACCENT_COLORS.some(c => c.id === v);
}

/** Stamp `data-accent` on <html> and remember it for the next load. */
export function applyAccentColor(accent: AccentColor): void {
  document.documentElement.setAttribute('data-accent', accent);
  try {
    localStorage.setItem(STORAGE_KEY, accent);
  } catch {
    // Storage blocked (private mode / sandboxed iframe) — the server
    // setting still applies once SWR resolves; only the pre-paint hint
    // is lost.
  }
}

/** Apply the cached accent (if any) before React's first render. */
export function applyCachedAccentColor(): void {
  let cached: string | null;
  try {
    cached = localStorage.getItem(STORAGE_KEY);
  } catch {
    return;
  }
  document.documentElement.setAttribute('data-accent', isAccent(cached) ? cached : DEFAULT_ACCENT);
}
