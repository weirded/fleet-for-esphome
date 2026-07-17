# Development Plans

Roadmap and bug tracking for Fleet for ESPHome, organized by release.

## Active files

Each WORKITEMS file's first paragraph is the authoritative theme — read the file, not this index.

- **[WORKITEMS-1.7.2.md](WORKITEMS-1.7.2.md)** — **Current release.** ESPHome 2026.7 support + polish (the Honest Gold tier flip + full i18n/German carried forward to 1.8).
- **[WORKITEMS-1.8.md](WORKITEMS-1.8.md)** — LLM assistance.
- **[WORKITEMS-1.9.md](WORKITEMS-1.9.md)** — ESPHome dashboard parity.
- **[WORKITEMS-future.md](WORKITEMS-future.md)** — Unscheduled backlog.
- **[SECURITY_AUDIT.md](SECURITY_AUDIT.md)** — Security audit findings.
- **[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)** — Release process.

## Archive

Historical release plans for versions already shipped. Themes only — details live in each file.

- **[archive/WORKITEMS-1.0.md](archive/WORKITEMS-1.0.md)** — First stable release.
- **[archive/WORKITEMS-1.1.md](archive/WORKITEMS-1.1.md)** — React UI + Monaco + HA integration.
- **[archive/WORKITEMS-1.2.md](archive/WORKITEMS-1.2.md)** — shadcn/ui design system + local worker.
- **[archive/WORKITEMS-1.3.md](archive/WORKITEMS-1.3.md)** — Quality + testing.
- **[archive/WORKITEMS-1.3.1.md](archive/WORKITEMS-1.3.1.md)** — Typed protocol + supply chain hardening.
- **[archive/WORKITEMS-1.4.md](archive/WORKITEMS-1.4.md)** — Fleet management + scheduled upgrades.
- **[archive/WORKITEMS-1.5.md](archive/WORKITEMS-1.5.md)** — Rebrand to ESPHome Fleet + native HA integration. <!-- br1-allow: 1.5 historical-rebrand description -->
- **[archive/WORKITEMS-1.6.md](archive/WORKITEMS-1.6.md)** — Per-file config history + Settings drawer.
- **[archive/WORKITEMS-1.6.1.md](archive/WORKITEMS-1.6.1.md)** — HA polish + Bronze→Silver.
- **[archive/WORKITEMS-1.6.2.md](archive/WORKITEMS-1.6.2.md)** — Install paths + AppArmor narrowing.
- **[archive/WORKITEMS-1.7.0.md](archive/WORKITEMS-1.7.0.md)** — heffneil-inspired device-management polish + fleet tags & routing.
- **[archive/WORKITEMS-1.7.1.md](archive/WORKITEMS-1.7.1.md)** — Brand refresh: "ESPHome Fleet" → "Fleet for ESPHome". <!-- br1-allow: rebrand-history pointer -->

## How this works

- Each release file mixes **work items** (planned features, marked `[x]` when done) and **bug fixes** (checkboxes with `**#NNN**` IDs and `*(X.Y.Z-dev.N)*` version tags).
- Bug numbers are global and monotonic across releases.
- The current release file contains **open bugs** at the bottom — these get folded into the main list as they land.
- When a release ships (merges to `main`), move its file to `archive/` and update the references in this README and `CLAUDE.md`.
