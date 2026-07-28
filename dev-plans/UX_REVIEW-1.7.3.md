# UX Review — 1.7.3 (`1.7.3`)

**Scope note.** 1.7.3 is a patch release with **zero UI source changes** — `git diff origin/main..develop -- ha-addon/ui/src ha-addon/ui/e2e` is empty. What *did* change is the built bundle: `tailwindcss` 4.3.0 → 4.3.3 landed in the DEP.1 sweep and every asset was rebuilt (`index-*.css` / `index-*.js` hashes all moved). So the right review for this release is **a rendering verification of the rebuilt bundle**, not a feature walkthrough of surfaces nobody touched. Carried-forward findings from `UX_REVIEW-1.7.2.md` are reconciled below.

## Method

Playwright against the deployed add-on on `hass-4` at `1.7.3` (`server-info` confirms `addon_version: 1.7.3`, `esphome_server_version: 2026.7.3`), Chromium, 1440×900 desktop and 390×844 mobile. Every primary tab visited and screenshotted, console and `pageerror` streams captured, document-level horizontal overflow measured.

Worth recording because it makes the sample a real one rather than a fixture: the fleet was **mid-upgrade during the review** — a manual bulk upgrade of 65 devices to ESPHome 2026.7.3, with all six workers compiling concurrently. So the tables were rendering live churn (state transitions, elapsed timers, worker assignment) rather than a static list, which is the harder case.

## Assessment

- **No regressions from the Tailwind bump.** All four tabs render correctly at 1440×900 — spacing, badge pills, tag chips, table borders, button variants and the dark palette all match 1.7.2. Nothing collapsed, mis-wrapped, or lost contrast.
- **Zero console errors and zero page errors** across the full session, including tab switches under 1 Hz SWR polling with 65 jobs in flight.
- **No document-level horizontal overflow** at desktop width.
- **Live-churn rendering is clean.** The Queue table repainted continuously (55 → 54 active during capture) without row jitter or column reflow; the Workers tab correctly grouped multi-slot workers (ai-mac, docker-optiplex-5, docker-pve at 2 slots each) with per-slot current-job rows.
- **`docs/screenshot.png` stays representative on layout, with one caveat.** Compared against today's live Devices tab: columns, toolbar buttons, filter chips, tag pills and per-row actions are all unchanged, and it still shows the canonical shape (Devices table + History drawer, diff view). The checklist's replace-it trigger — *"columns, toolbar buttons, badges, or layout have changed meaningfully"* — is therefore not met. The caveat is that its header badge reads **`v1.7.1-dev.12`**, a dev build, which is scruffy for the README's primary hook. Deliberately **not** refreshed in this release: the only available capture target was mid-way through a 65-device bulk upgrade, so every row read *Compiling + OTA* — a materially worse hero image than a slightly stale version chip. Recapture via `scripts/capture-readme-screenshot.js` once the fleet is idle.

## Findings

**UX.1 (carried forward from 1.7.2, unresolved) — the Language picker still offers *Deutsch*, which is still a silent no-op.** Re-verified rather than assumed: `src/i18n/locales/de.json` is **3 bytes / 0 keys**, and 10 `I18N.*` items remain unchecked in `WORKITEMS-1.8.md`. `i18next` falls back to English, so selecting **Deutsch** changes nothing. Unchanged in 1.7.3 by scope — this release deliberately touched no UI source. Restated verbatim for 1.8, where the I18N work lands.

**UX.2 (new, minor) — the Language row's help text names a release that has already shipped.** It reads *"Interface language. Translations land progressively across 1.7.2."* (`SettingsDrawer.tsx:247`). 1.7.2 shipped in July and the translations did not land in it; they're queued for 1.8. A user reading this in 1.7.3 is told to expect something from a version they're already past. One-line copy fix, but it needs a rebuild + full matrix re-verification, which isn't worth reopening a cut-and-verified release for. Bundle it with the I18N.* work in 1.8, which has to revisit this string anyway.

**UX.3 (new, pre-existing — not a 1.7.3 regression) — two mobile-width layout defects at 390 px.** Both predate this release (no UI source changed), surfaced here because the mobile viewport was re-checked as part of verifying the rebuilt bundle:
1. **The top header bar clips instead of wrapping or scrolling.** The ESPHome version chip truncates mid-word (`ESPHome 202…`) at the viewport edge, and the controls to its right (Secrets, theme, streamer-mode, ESPHome Web, settings) are simply unreachable — there's no horizontal scroll affordance on that bar, so on a phone those actions can't be invoked at all.
2. **The Devices table header has fewer cells than its body rows.** At 390 px the header collapses to `Device | Tags` while each row still renders `Device | tag | Upgrade | Edit | ⋮`, so the action buttons sit under the *Tags* heading. Functional, but the header stops describing the columns.

Neither blocks the release. Both are worth a mobile-pass workitem rather than a spot fix, since the underlying cause is that the responsive strategy drops header cells and body cells independently.

## Prioritized recommendations (for 1.8 to pick from)

| ID | Finding | Suggested action |
|----|---------|------------------|
| UX.1 | Language picker's *Deutsch* option is still a silent no-op (`de.json` = 0 keys) | Disable non-English options with a tooltip until I18N.4/I18N.9 land, or accept as harmless (the **Auto** default masks it for non-German browsers) |
| UX.2 | Language help text still promises translations "across 1.7.2" | One-line copy fix; fold into the I18N.* work, which rewrites this string anyway |
| UX.3 | Mobile (390 px): header bar clips with no scroll affordance; Devices table header has fewer cells than body rows | Treat as one mobile-pass workitem — make the header bar wrap or scroll, and keep header/body cell counts in sync at every breakpoint |
