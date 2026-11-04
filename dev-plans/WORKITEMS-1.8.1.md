# Work Items — 1.8.1

Theme: **Patch fast-follow to 1.8.0: a git-packages secrets fix and a README that's honest about the new ESPHome Device Builder.** ESPHome 2026.6 made the Device Builder its default dashboard, and by 1.26 it matches much of what the README said only Fleet had (job queue, remote build servers, labels, table view, bulk update). 1.8.1 rewrites that pitch around what is still unique, and ships the #154 fix that users on older ESPHome pins can't work around. No new features — a patch by semver.

**A note on the `-dev.N` tags below.** As with 1.7.3, this work is built on `develop` under the `1.9.0-dev.N` placeholder that `21d7f39` set when the 1.9 cycle opened. It's renumbered to 1.8.1 at cut time (`bash scripts/bump-version.sh 1.8.1`); the tags stay as the versions actually built and tested. `WORKITEMS-1.9.md`'s scope is untouched.

---

## Bug fixes

- [x] **#318** *(1.9.0-dev.3)* — ([GitHub #154](https://github.com/weirded/fleet-for-esphome/issues/154)) Git-URL `packages:` whose `!secret` refs live only in the remote package failed on every worker with "Error reading file …/secrets.yaml". Root cause is upstream: ESPHome 2026.4–2026.7's `ConfigBundleCreator` scanned only local YAML for `!secret`, so the bundle shipped no `secrets.yaml`; fixed in ESPHome 2026.8.0 (esphome/esphome#18053, `add_secret_scan_dir`). Reproduced on hass-4 with the reporter's exact YAML: 2026.4.5 bundle = `manifest.json` + target only; 2026.10.2 bundle correct. Backport for servers bundling with <2026.8: the bundle subprocess wraps `esphome.git.clone_or_update` (only when `add_secret_scan_dir` is absent) and reports each `packages` checkout dir on stderr; `scanner.merge_remote_package_secrets` scans those checkouts (minus `.git/`) for `!secret` keys and merges the missing ones from `<config>/secrets.yaml` into the bundle. Verified on hass-4: 2026.4.5 bundle now carries the keys and validates worker-side; 2026.10.2 bundle unchanged. Regression: `tests/test_bundle_remote_package_secrets.py`.

## Docs

- [x] **RM.1** *(1.9.0-dev.2)* — README "Why use this instead of the ESPHome Device Builder?" rewritten against Device Builder 1.26 / ESPHome 2026.10: dropped claims the stock builder now matches (job queue, remote builds, labels, table view, bulk update), leading with version pinning, scheduled upgrades, firmware archive, pairing-free workers with direct OTA + add-on fallback, routing rules, restore UI, HA entities.

## Bug fixes (continued)

- [x] **#319** *(1.9.0-dev.4)* — ([GitHub #257](https://github.com/weirded/fleet-for-esphome/issues/257)) *After upgrading to 1.8.0, the new **Latest ESPHome version** / **ESPHome update available** entities never appeared.* Reporter's screenshot shows the hub device on 1.8.0 with only the 1.7-era sensors. Root cause: the add-on copies the updated integration into `/config/custom_components/esphome_fleet/` on boot, but HA Core loads custom integrations once at startup, so it keeps running the old code (and old entity set) until restarted — and the only signal was an INFO line in the add-on log. Every add-on upgrade hits this, since the manifest version tracks the add-on version. **Fix:** when `install_integration()` returns `"updated"`, `notify_restart_required()` checks `GET /core/api/config` via the Supervisor proxy and, only if `esphome_fleet` is in the loaded `components`, posts a persistent notification ("Restart Home Assistant to finish updating Fleet for ESPHome", fixed `notification_id` so restarts don't stack; HA drops it on restart, exactly when it stops being true). Fire-and-forget task, best-effort, never raises; skipped in standalone (no `SUPERVISOR_TOKEN`). Regression: 5 tests in `tests/test_integration_installer_logic.py` against a fake core-API server. Reply on GitHub #257 asks the reporter to restart HA; issue stays open until confirmed.

## Dependencies

- [x] **DEP.1** *(1.9.0-dev.4)* — merged [weirded/fleet-for-esphome#304](https://github.com/weirded/fleet-for-esphome/pull/304): server `aioesphomeapi` `>=46.25.0,<47`, lock → **46.26.0** (+ new transitive `backports-zstd`). This is the deferred **DEP.3** evaluation from `WORKITEMS-future.md`: AST-diffed 45.13.1 vs 46.26.0 for every call we make (`APIClient(...)`, `connect`, `device_info`, `subscribe_logs`, `list_entities_services`, `button_command`, `disconnect`) — identical except one new optional `__init__` kwarg and one new `DeviceInfo` field; the `"disconnect request failed"` string our log filter matches still exists; we register no connection-closed callbacks, so 46.0's `ConnectionClosedEvent` doesn't touch us. Also confirmed the server really runs its own pin: `device_poller` imports `aioesphomeapi` at module load, before the ESPHome venv (which carries ESPHome 2026.10.2's `aioesphomeapi==46.12.0`) is prepended to `sys.path`. Real-device check on hass-4 after deploy: see test-matrix notes below. `requirements.txt` comment updated; client lock unchanged → no `IMAGE_VERSION` bump.
- [x] **DEP.2** *(1.9.0-dev.4)* — Dependabot alerts: `minimatch` GHSA-g3hv-75wg-m249 (moderate, `>=7 <11.0.1`) fixed with an npm `overrides` entry `^11.0.4` (eslint / typescript-eslint / ts-morph all resolve to 11.0.4; build + `npm audit --omit=dev` clean; eslint's existing react-hooks findings unchanged). `deepmerge` GHSA-gcqx-w48m-w2h4 (low, no fixed version) **accepted** 2026-11-04: reached only via the `shadcn` CLI (dev dependency, never in the shipped bundle), and the prototype-poisoning path needs attacker-controlled input to the CLI's own config merge.

## Community

- [ ] **PR.315** — [weirded/fleet-for-esphome#315](https://github.com/weirded/fleet-for-esphome/pull/315) (anddon): worker computes `cache_size_mb` from the background disk-usage sampler instead of walking `cache/` + `slots/` every heartbeat (~3 % of a core → ~0 on idle workers), and adds `cached_targets` / `cache_size_mb` to `SystemInfo` so the Workers tab cache line finally renders (the UI already reads both; the server was dropping them). Additive optional protocol fields (PY-6 OK); `.py`-only → reaches workers via source auto-update, no image bump. Its earlier red `audit`/`frontend` checks were a stale base (pre-1.8.0 aiohttp/DOMPurify); branch updated onto `develop`, fork workflows approved.

## Not in this release

- **`@tanstack/react-table` 9** ([#295](https://github.com/weirded/fleet-for-esphome/pull/295)) and **`typescript` 7** ([#225](https://github.com/weirded/fleet-for-esphome/pull/225)) — majors, kept out of a patch cut. Stay open.
- Everything in `WORKITEMS-1.9.md`.
- `test_device_poller.py`'s whole-library `MagicMock` stub (the blind spot DEP.3 called out) stays — replacing it with a narrow fake is worthwhile but not patch material.

## Release checklist notes (patch-sized)

Run `dev-plans/RELEASE_CHECKLIST.md` in full; patch-specific calls:

- [ ] ESPHome pin: already at latest stable 2026.10.2 — re-check PyPI at cut time.
- [ ] `CHANGELOG.md` `## 1.8.1`: user-facing — git `packages:` with secrets now build on any ESPHome version; README comparison refreshed (one line, under-the-hood).
- [ ] `SECURITY.md`: 1.8.1 → current, 1.8.0 → previous stable, drop 1.7.3.
- [ ] `SECURITY_AUDIT.md` refresh note: #318 merges extra `!secret` keys into worker bundles on ESPHome <2026.8 — only keys the remote package references, so the "workers receive only referenced keys" claim in `SECURITY.md` still holds; say so explicitly.
- [ ] UX review: skipped for a patch (1.7.3 precedent) unless PR.315 lands, in which case spot-check the Workers-tab cache line.
- [ ] `docs/screenshot.png`: unchanged UI, no refresh needed.
- [ ] After tag: move this file to `archive/`, bump `develop` back onto `1.9.0-dev.N`, update `dev-plans/README.md`.
