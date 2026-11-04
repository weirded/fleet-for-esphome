# Work Items — 1.8.1

Theme: **Patch fast-follow to 1.8.0: a git-packages secrets fix and a README that's honest about the new ESPHome Device Builder.** ESPHome 2026.6 made the Device Builder its default dashboard, and by 1.26 it matches much of what the README said only Fleet had (job queue, remote build servers, labels, table view, bulk update). 1.8.1 rewrites that pitch around what is still unique, and ships the #154 fix that users on older ESPHome pins can't work around. No new features — a patch by semver.

**A note on the `-dev.N` tags below.** As with 1.7.3, this work is built on `develop` under the `1.9.0-dev.N` placeholder that `21d7f39` set when the 1.9 cycle opened. It's renumbered to 1.8.1 at cut time (`bash scripts/bump-version.sh 1.8.1`); the tags stay as the versions actually built and tested. `WORKITEMS-1.9.md`'s scope is untouched.

---

## Bug fixes

- [x] **#318** *(1.9.0-dev.3)* — ([GitHub #154](https://github.com/weirded/fleet-for-esphome/issues/154)) Git-URL `packages:` whose `!secret` refs live only in the remote package failed on every worker with "Error reading file …/secrets.yaml". Root cause is upstream: ESPHome 2026.4–2026.7's `ConfigBundleCreator` scanned only local YAML for `!secret`, so the bundle shipped no `secrets.yaml`; fixed in ESPHome 2026.8.0 (esphome/esphome#18053, `add_secret_scan_dir`). Reproduced on hass-4 with the reporter's exact YAML: 2026.4.5 bundle = `manifest.json` + target only; 2026.10.2 bundle correct. Backport for servers bundling with <2026.8: the bundle subprocess wraps `esphome.git.clone_or_update` (only when `add_secret_scan_dir` is absent) and reports each `packages` checkout dir on stderr; `scanner.merge_remote_package_secrets` scans those checkouts (minus `.git/`) for `!secret` keys and merges the missing ones from `<config>/secrets.yaml` into the bundle. Verified on hass-4: 2026.4.5 bundle now carries the keys and validates worker-side; 2026.10.2 bundle unchanged. Regression: `tests/test_bundle_remote_package_secrets.py`.

## Docs

- [x] **RM.1** *(1.9.0-dev.2)* — README "Why use this instead of the ESPHome Device Builder?" rewritten against Device Builder 1.26 / ESPHome 2026.10: dropped claims the stock builder now matches (job queue, remote builds, labels, table view, bulk update), leading with version pinning, scheduled upgrades, firmware archive, pairing-free workers with direct OTA + add-on fallback, routing rules, restore UI, HA entities.

## Proposed scope — needs a yes/no before the cut

- [ ] **PR.315** — merge [weirded/fleet-for-esphome#315](https://github.com/weirded/fleet-for-esphome/pull/315) (community, anddon): worker computes `cache_size_mb` from the background disk-usage sampler instead of walking `cache/` + `slots/` every heartbeat (~3 % of a core → ~0 on idle workers), and adds `cached_targets` / `cache_size_mb` to `SystemInfo` so the Workers tab cache line finally renders. Additive optional protocol fields (PY-6 OK); `.py`-only so it reaches workers via source auto-update without an image bump. Its `audit` + `frontend` checks are red — confirm that's a stale base (rebase onto `develop`), not the change.
- [ ] **DEP.1** — merge [weirded/fleet-for-esphome#304](https://github.com/weirded/fleet-for-esphome/pull/304) (`aioesphomeapi` ≥46.25 for the server, CI green). Rerun `scripts/refresh-deps.sh` for PY-8; server-only, so no `IMAGE_VERSION` bump unless the client lock moves.
- [ ] **DEP.2** — Dependabot alerts: `minimatch` (moderate, fix 11.0.1) and `deepmerge` (low, no fix). Both reach us only through dev tooling (eslint, typescript-eslint, shadcn CLI, ts-morph) and never ship in the bundle. Either bump `minimatch` via an npm `overrides` entry if eslint tolerates 11.x, or accept-risk both with a dated note (no high/critical open → release gate passes either way).
- [ ] **GH.257** — [weirded/fleet-for-esphome#257](https://github.com/weirded/fleet-for-esphome/issues/257) ("HA notification when new ESP version is available") looks already covered by 1.8.0's **Latest ESPHome version** sensor + update-available binary sensor (`ha-addon/DOCS.md` has the automation example). Reply with the pointer and close, pending maintainer OK.

## Not in this release

- **`@tanstack/react-table` 9** ([#295](https://github.com/weirded/fleet-for-esphome/pull/295)) and **`typescript` 7** ([#225](https://github.com/weirded/fleet-for-esphome/pull/225)) — majors, kept out of a patch cut. Stay open.
- Everything in `WORKITEMS-1.9.md`.

## Release checklist notes (patch-sized)

Run `dev-plans/RELEASE_CHECKLIST.md` in full; patch-specific calls:

- [ ] ESPHome pin: already at latest stable 2026.10.2 — re-check PyPI at cut time.
- [ ] `CHANGELOG.md` `## 1.8.1`: user-facing — git `packages:` with secrets now build on any ESPHome version; README comparison refreshed (one line, under-the-hood).
- [ ] `SECURITY.md`: 1.8.1 → current, 1.8.0 → previous stable, drop 1.7.3.
- [ ] `SECURITY_AUDIT.md` refresh note: #318 merges extra `!secret` keys into worker bundles on ESPHome <2026.8 — only keys the remote package references, so the "workers receive only referenced keys" claim in `SECURITY.md` still holds; say so explicitly.
- [ ] UX review: skipped for a patch (1.7.3 precedent) unless PR.315 lands, in which case spot-check the Workers-tab cache line.
- [ ] `docs/screenshot.png`: unchanged UI, no refresh needed.
- [ ] After tag: move this file to `archive/`, bump `develop` back onto `1.9.0-dev.N`, update `dev-plans/README.md`.
