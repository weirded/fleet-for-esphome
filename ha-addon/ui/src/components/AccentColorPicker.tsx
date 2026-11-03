import { Check } from 'lucide-react';

import { ACCENT_COLORS, type AccentColor } from '@/lib/accent';

// #150 — accent-colour swatch row for the Settings drawer (Display).
//
// Presets only (no free-form picker) so every option has hand-tuned
// light + dark values in theme.css. Each swatch carries its own
// `data-accent` attribute, which re-scopes `--accent` to that preset
// for the swatch element only — the preview colour therefore always
// matches what the preset will look like in the current theme, with
// the hex values living in exactly one place (theme.css).
export function AccentColorPicker({
  value,
  onChange,
}: {
  value: AccentColor;
  onChange: (v: AccentColor) => void;
}) {
  return (
    <div role="group" aria-label="Accent colour" className="flex flex-wrap gap-1.5">
      {ACCENT_COLORS.map(c => {
        const selected = c.id === value;
        return (
          <button
            key={c.id}
            type="button"
            data-accent={c.id}
            aria-label={c.label}
            title={c.label}
            aria-pressed={selected}
            onClick={() => {
              if (!selected) onChange(c.id);
            }}
            className={`inline-flex size-6 cursor-pointer items-center justify-center rounded-full border-2 bg-[var(--accent)] text-white transition-shadow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--surface)] ${selected ? 'border-[var(--text)]' : 'border-transparent hover:border-[var(--text-muted)]'}`}
          >
            {selected && <Check className="size-3.5" strokeWidth={3} aria-hidden="true" />}
          </button>
        );
      })}
    </div>
  );
}
