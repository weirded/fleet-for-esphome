import { defineConfig } from '@playwright/test';

/**
 * Playwright config for smoke tests against any deployed Fleet instance:
 * hass-4, a HAOS VM, or a standalone Docker container. The same suite runs
 * against all three — targets that don't have HA skip `@requires-ha` specs
 * via `--grep-invert=@requires-ha`.
 *
 * Run with:
 *   npm run test:e2e:hass-4
 *
 * Env (FLEET_* takes precedence; HASS4_* kept as BC fallback so
 * push-to-hass-4.sh and push-to-haos.sh keep working unchanged):
 *
 *   FLEET_URL / HASS4_URL       — base URL of the add-on's HTTP port
 *                                 (default http://192.168.225.112:8765).
 *   FLEET_TOKEN / HASS4_ADDON_TOKEN
 *                               — add-on's shared Bearer token. Required
 *                                 when `require_ha_auth=true` (the default
 *                                 on 1.5+). Attached to every fetch/XHR
 *                                 the browser makes AND to the `request`
 *                                 fixture via `extraHTTPHeaders` below.
 */
const fleetToken = process.env.FLEET_TOKEN || process.env.HASS4_ADDON_TOKEN || '';

// scripts/test-matrix.py collates three parallel runs via per-target JSON
// reports. When PLAYWRIGHT_JSON_OUTPUT_NAME is set, add the json reporter
// alongside list+html. The json reporter writes to that env-var path.
type ReporterEntry = string | [string] | [string, Record<string, unknown>];
const reporters: ReporterEntry[] = [
  ['list'],
  ['html', { outputFolder: 'playwright-report', open: 'never' }],
];
if (process.env.PLAYWRIGHT_JSON_OUTPUT_NAME) {
  reporters.push(['json']);
}

export default defineConfig({
  testDir: '.',
  // Compile + OTA can take a while on real hardware
  timeout: 10 * 60_000,
  expect: { timeout: 30_000 },
  retries: 0,
  // Run serially — they touch real state, so don't parallelize
  workers: 1,
  fullyParallel: false,
  reporter: reporters,
  // Per-target artifacts when run from scripts/test-matrix.py in parallel.
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR || 'test-results',
  use: {
    baseURL: process.env.FLEET_URL || process.env.HASS4_URL || 'http://192.168.225.112:8765',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
    // Real network may be slower than localhost
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    // AU.7: /ui/api/* requires a Bearer on port 8765 since 1.5.0.
    // `extraHTTPHeaders` applies to the `request` fixture AND to every
    // fetch/XHR the browser page makes (including SWR polls) — both the
    // Playwright APIRequestContext and the BrowserContext pick it up.
    extraHTTPHeaders: fleetToken
      ? { Authorization: `Bearer ${fleetToken}` }
      : {},
  },
});
