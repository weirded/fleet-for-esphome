# UX Review — 1.7.2 (`1.7.2`)

**Scope note.** 1.7.2 shipped as a focused *ESPHome 2026.7 support + polish* release (see `WORKITEMS-1.7.2.md`). The user-facing UI delta versus 1.7.1 is small and additive — three new controls, all following existing shadcn patterns — so this is a **light-touch review of the changed surfaces**, not a full end-to-end walkthrough. The prior "Honest Gold" UI work that would have warranted a full re-review was carried forward to 1.8. No prior `UX_REVIEW-*.md` existed at the active path, so there are no carried-forward findings to reconcile.

## Changed surfaces reviewed

Derived from `git diff v1.7.1 -- ha-addon/ui/src/` — the only touched components are:

- **`SettingsDrawer.tsx`** — two new `EnumRow`s under **Display**: **Font size** (Small / Normal / Large, #145) and **Language** (Auto / English / Deutsch, I18N.2).
- **`EsphomeVersionDropdown.tsx`** + **`UpgradeModal.tsx`** — a new **Installable only** filter toggle (default ON) alongside the existing **Show betas** toggle (#131).
- **`App.tsx`** / **`i18n/`** — non-visual wiring (i18next provider, `data-font-size` attribute stamping, `resolveLanguage` for `auto`).

The Devices table, Queue, Workers, Schedules tabs, all modals, the per-row hamburger, and the mobile/light/streamer surfaces are **unchanged** this release — no re-review needed, and `docs/screenshot.png` (Devices tab + History drawer) stays representative.

## Assessment

- **Font size (Small / Normal / Large).** Consistent with the existing Display `EnumRow`s (time format, theme). Scales the root rem so tables/buttons/dialogs move together — verified in the #145 workitem. Default **Normal** renders byte-identical to 1.7.1. No finding.
- **Installable-only filter.** Mirrors the adjacent **Show betas** toggle exactly (same control, same placement, tooltip names the 2023.7.0 floor). Default ON hides versions that would fail `pip install`, which is the safe default. Follows the "disable/hide unavailable, explain why" principle. No finding.

## Findings

**UX.1 — The Language picker offers *Deutsch*, but German isn't translated yet, so selecting it silently renders English.** 1.7.2 shipped only the i18n *foundation* (I18N.1/I18N.2); the German catalog (`de.json`) is effectively empty and string extraction (I18N.4) is carried to 1.8, so `i18next` falls back to English (`fallbackLng: 'en'`). A user who picks **Deutsch** sees no change — a control that looks functional but is a no-op, which tends to generate "German translation is broken" reports. **Recommendation (pick one, for 1.8's I18N landing or a 1.7.2 follow-up):** (a) hide the **Language** row (or the non-English options) behind a build flag until `de.json` is populated; or (b) keep it but disable the non-English options with a tooltip ("German translation in progress"); or (c) accept it as harmless (default is **Auto**, which resolves to the browser locale and, for non-German browsers, English — so most users never hit the dead option). Left unfixed in 1.7.2 by scope decision (the release is the urgent ESPHome fix); documented here so it's a conscious choice, not an oversight.

## Prioritized recommendations (for 1.8 to pick from)

| ID | Finding | Suggested action |
|----|---------|------------------|
| UX.1 | Language picker's German option is a silent no-op until `de.json` lands | Hide/disable non-English options until I18N.4/I18N.9 complete, or accept as harmless (Auto default masks it) |
