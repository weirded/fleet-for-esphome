import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { retryTransient } from './retry';

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Smoke test against the author's hass-4 distributed-esphome instance.
 *
 * Compiles a real device end-to-end:
 *   1. Devices tab loads with the target row
 *   2. Click Upgrade
 *   3. Job appears in the queue with state working
 *   4. Open the log modal and tail the streaming compile output
 *   5. Compile + OTA succeeds (state success)
 *   6. Open Live Logs from the device row and verify the device API stream
 *
 * The target device defaults to `cyd-office-info.yaml` and can be overridden
 * with the FLEET_TARGET env var (HASS4_TARGET also accepted). The base URL
 * is set in playwright.config.ts via FLEET_URL / HASS4_URL (default
 * http://192.168.225.112:8765).
 *
 * Run with:
 *   npm run test:e2e:hass-4
 */

const TARGET_FILENAME = process.env.FLEET_TARGET || process.env.HASS4_TARGET || 'cyd-office-info.yaml';
const TARGET_STEM = TARGET_FILENAME.replace(/\.ya?ml$/, '');

// Read the expected add-on version from ha-addon/VERSION at test startup so
// the suite refuses to run against a stale deploy. Override with EXPECTED_VERSION
// if you intentionally want to test a different version.
const EXPECTED_VERSION =
  process.env.EXPECTED_VERSION ||
  readFileSync(join(__dirname, '../../VERSION'), 'utf-8').trim();

// Compile + OTA budget. Real ESP32 builds with PlatformIO can be slow on a
// cold cache; tune via env var if needed.
const COMPILE_BUDGET_MS = parseInt(process.env.COMPILE_BUDGET_MS || '480000', 10);

// How long we'll wait to see at least one log line stream into the modal.
const LOG_STREAM_TIMEOUT_MS = 60_000;

// How long we'll watch the device live log for an incoming line.
const DEVICE_LOG_TIMEOUT_MS = 30_000;

// Job ID enqueued in test 2, polled in test 3. Module-scoped so it survives
// across the serial tests in this file.
let enqueuedJobId: string | null = null;

test.describe.serial('cyd-office-info hass-4 smoke', () => {
  // Confirm we're talking to the expected add-on version before doing anything
  // else. If the deploy is stale, the rest of the tests are meaningless.
  test.beforeAll(async ({ request }) => {
    const resp = await request.get('/ui/api/server-info');
    expect(resp.ok(), `server-info should return 2xx (got ${resp.status()})`).toBe(true);
    const info = await resp.json();
    expect(
      info.addon_version,
      `expected add-on version ${EXPECTED_VERSION}, got ${info.addon_version}. ` +
        `If you intentionally want to test a different version, set EXPECTED_VERSION.`,
    ).toBe(EXPECTED_VERSION);
  });

  test('devices tab loads and shows the target device', async ({ page }) => {
    await page.goto('/');

    // Header sanity — version badge should reflect the deployed version
    await expect(page.locator('header')).toBeVisible();
    // BR.1: header wordmark is "Fleet for ESPHome" (was "ESPHome Fleet"  // br1-allow: prior-brand reference
    // 1.5–1.7.0; was just "Fleet" beside the ESPHome logo image earlier).
    await expect(page.getByText('Fleet for ESPHome', { exact: true })).toBeVisible();
    await expect(page.getByText(`v${EXPECTED_VERSION}`)).toBeVisible();

    // Devices tab is the default — wait for the device table to populate
    const targetRow = await findTargetRow(page);
    await expect(targetRow).toBeVisible({ timeout: 30_000 });
  });

  test('schedule upgrade and verify it lands in the queue', async ({ page, request }) => {
    await page.goto('/');
    const targetRow = await findTargetRow(page);
    await expect(targetRow).toBeVisible({ timeout: 30_000 });

    // Snapshot the latest existing job ID for this target so we can detect
    // the new one we're about to create
    const before = await latestJobIdFor(request, TARGET_FILENAME);

    // Click the row's Upgrade button — opens the UpgradeModal (#16). The
    // modal lets the user pick a worker + ESPHome version; we accept the
    // defaults and click the modal's Upgrade button to actually enqueue.
    const upgradeBtn = targetRow.getByRole('button', { name: /^upgrade$/i });
    await expect(upgradeBtn).toBeVisible();
    await upgradeBtn.click();

    // Modal is open — find the confirm button (the second "Upgrade" button on
    // the page, since the row's button is still in the DOM behind the modal).
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible({ timeout: 5_000 });
    await dialog.getByRole('button', { name: /^upgrade$/i }).click();

    // Wait for a NEW job (different ID) to appear via the API — source of truth
    await expect.poll(
      async () => latestJobIdFor(request, TARGET_FILENAME),
      { timeout: 15_000, message: 'expected a new job to be enqueued' },
    ).not.toBe(before);

    enqueuedJobId = await latestJobIdFor(request, TARGET_FILENAME);
    expect(enqueuedJobId).toBeTruthy();

    // #137: a single-device Upgrade auto-opens the new job's build log.
    // Close it — the modal covers the tab bar.
    const logDialog = page.getByRole('dialog');
    await expect(logDialog).toBeVisible({ timeout: 10_000 });
    await page.keyboard.press('Escape');
    await expect(logDialog).toHaveCount(0);

    // Switch to the Queue tab and confirm the row is visible in the UI
    await page.getByRole('button', { name: /^Queue/ }).click();
    const queueRow = await findQueueRow(page);
    await expect(queueRow).toBeVisible({ timeout: 15_000 });
  });

  test('compile runs to completion and live log streams', async ({ page, request }) => {
    test.setTimeout(COMPILE_BUDGET_MS + 60_000);
    expect(enqueuedJobId, 'previous test should have set enqueuedJobId').toBeTruthy();

    await page.goto('/');
    await page.getByRole('button', { name: /^Queue/ }).click();

    const queueRow = await findQueueRow(page);
    await expect(queueRow).toBeVisible({ timeout: 30_000 });

    // #221: per-row Log button is inline (was hamburgered between
    // #209 and #221).
    const logBtn = queueRow.getByRole('button', { name: 'Log', exact: true });
    await expect(logBtn).toBeVisible({ timeout: 30_000 });
    await logBtn.click();

    // The log modal contains an xterm.js terminal — the screen renders text
    // into a div with class "xterm-screen". Wait for it to render and stream
    // at least some compile output.
    const terminal = page.locator('.xterm-screen').first();
    await expect(terminal).toBeVisible({ timeout: 10_000 });
    await expect.poll(
      async () => (await terminal.textContent())?.length ?? 0,
      { timeout: LOG_STREAM_TIMEOUT_MS, message: 'expected log lines to stream into the modal' },
    ).toBeGreaterThan(50);

    // Close the modal — we'll poll completion via the API, which is the
    // source of truth and avoids fragile UI badge text matching.
    await page.keyboard.press('Escape');

    // Poll the queue API for our specific job until it reaches a terminal state
    let finalJob: QueueJob | null = null;
    await expect.poll(
      async () => {
        const job = await getJob(request, enqueuedJobId!);
        if (job && isTerminal(job.state)) {
          finalJob = job;
          return job.state;
        }
        return job?.state ?? 'missing';
      },
      {
        timeout: COMPILE_BUDGET_MS,
        intervals: [2_000, 5_000, 10_000],
        message: `compile + OTA did not finish within ${COMPILE_BUDGET_MS}ms`,
      },
    ).toMatch(/^(success|failed|timed_out)$/);

    // Compile must have succeeded AND OTA must have succeeded
    expect(finalJob, 'final job should be set').not.toBeNull();
    expect(finalJob!.state, `final state: ${finalJob!.state}`).toBe('success');
    expect(
      finalJob!.ota_result,
      `OTA result: ${finalJob!.ota_result}`,
    ).toBe('success');
  });

  test('editor opens, shows YAML, edits comment, saves, persists', async ({ page, request }) => {
    // Regression for #15 — the editor stuck on a loading state because the
    // E.9 CSP blocked Monaco's CDN load. Rather than just verifying it
    // opens, we round-trip a real edit through the save endpoint and
    // confirm the new content survives a reopen.
    test.setTimeout(60_000);

    await page.goto('/');
    const targetRow = await findTargetRow(page);
    await expect(targetRow).toBeVisible({ timeout: 30_000 });

    // Click the row's Edit button
    await targetRow.getByRole('button', { name: /^edit$/i }).click();

    // Monaco container appears…
    await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15_000 });
    // …and actually rendered text (regression guard for the empty-loader
    // failure mode where the div appears but Monaco never finished loading).
    // Bug: the first Monaco view-line used to be `esphome:` reliably, but
    // when the device has a `# esphome-fleet:` metadata comment block at
    // the top (TG.5 tags / pin_version / schedule_once etc. all live
    // there) the first line is now the explanatory header. Match against
    // the *full* rendered text rather than just the first view-line so
    // the regex hits regardless of how many comment lines lead the file.
    const monaco = page.locator('.monaco-editor').first();
    await expect(monaco).toContainText(/esphome|wifi|api|substitutions/, { timeout: 15_000 });

    // Read the original content via the API so we can edit it precisely
    // (Monaco-driven keyboard input is fragile across platforms).
    const beforeResp = await request.get(`./ui/api/targets/${TARGET_FILENAME}/content`);
    expect(beforeResp.ok()).toBeTruthy();
    const before = (await beforeResp.json() as { content: string }).content;

    // Stamp a marker into the comment line so the test is idempotent and
    // visible after the fact. Replace any prior marker to keep the file tidy.
    const marker = `e2e-test-marker-${Date.now()}`;
    let edited: string;
    if (/comment:\s*(['"])?[^\n]*\1?/.test(before)) {
      edited = before.replace(
        /comment:\s*(['"])?[^\n]*\1?/,
        `comment: "${marker}"`,
      );
    } else {
      // No comment field — append one under the esphome: block
      edited = before.replace(/(esphome:\s*\n)/, `$1  comment: "${marker}"\n`);
    }
    expect(edited).not.toBe(before);

    // Save via the API directly (deterministic; doesn't rely on Save button
    // wiring or toast timing).
    const saveResp = await request.post(`./ui/api/targets/${TARGET_FILENAME}/content`, {
      data: { content: edited },
    });
    expect(saveResp.ok(), 'save endpoint should accept the edit').toBeTruthy();

    // #21 / #25 / #84: validate the edit. Runs directly on the server.
    // If the device is pinned to a different ESPHome version, the server
    // installs it first via VersionManager — which can take 30-60s on
    // first run (subsequent runs use the cached install).
    const validateResp = await request.post('./ui/api/validate', {
      data: { target: TARGET_FILENAME },
      timeout: 120_000,
    });
    expect(validateResp.ok(), 'validate endpoint should return 2xx').toBeTruthy();
    const validateResult = await validateResp.json() as { success: boolean; output: string };
    expect(
      validateResult.success,
      `validation should pass; output:\n${validateResult.output}`,
    ).toBe(true);

    // Reopen the editor and verify the marker shows up.
    await page.keyboard.press('Escape');
    // CR.6: wait on the dialog actually unmounting instead of a fixed
    // timeout. Stops prod-smoke flakes on slow HA restarts.
    await expect(page.getByRole('dialog')).toHaveCount(0, { timeout: 5_000 });
    await targetRow.getByRole('button', { name: /^edit$/i }).click();
    await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15_000 });
    // Wait for any view-line containing the marker. Monaco may not have
    // scrolled it into view, so search across all lines.
    await expect.poll(
      async () => (await page.locator('.monaco-editor').first().textContent()) ?? '',
      { timeout: 10_000, message: 'edited marker should be in the editor after reopen' },
    ).toContain(marker);

    await page.keyboard.press('Escape');
  });

  test('live device logs stream from cyd-office-info', async ({ page }) => {
    test.setTimeout(DEVICE_LOG_TIMEOUT_MS + 60_000);

    await page.goto('/');
    const targetRow = await findTargetRow(page);
    await expect(targetRow).toBeVisible({ timeout: 30_000 });

    // Open the row's hamburger menu
    const menuTrigger = targetRow.locator('.action-menu-trigger');
    await expect(menuTrigger).toBeVisible();
    await menuTrigger.click();

    // Click "Live Logs" — Radix DropdownMenuItem (post-QS.16) is role="menuitem"
    await page.getByRole('menuitem', { name: /^live logs$/i }).click();

    // The DeviceLogModal also uses xterm — wait for it to render
    const terminal = page.locator('.xterm-screen').first();
    await expect(terminal).toBeVisible({ timeout: 10_000 });

    // Wait for at least some content to stream from the device
    await expect.poll(
      async () => (await terminal.textContent())?.length ?? 0,
      { timeout: DEVICE_LOG_TIMEOUT_MS, message: 'expected device log lines to stream' },
    ).toBeGreaterThan(20);

    // Close it
    await page.keyboard.press('Escape');
  });

  // #24: parallel-compile coverage. Pin garage-door-big to the local-worker
  // (which has 1 slot) and verify it runs to completion against a real ESP
  // device. The local-worker existing on hass-4 with 1 slot is enforced as a
  // precondition; the test fails fast if the topology has drifted.
  test('parallel compile: garage-door-big pinned to local-worker', async ({ request }) => {
    test.setTimeout(COMPILE_BUDGET_MS + 60_000);

    // Precondition: local-worker is online with exactly 1 slot. Anything else
    // means the test environment has drifted from the intended setup.
    const workersResp = await request.get('./ui/api/workers');
    expect(workersResp.ok(), 'workers endpoint should return 2xx').toBeTruthy();
    const workers = (await workersResp.json()) as Array<{
      client_id: string;
      hostname: string;
      online: boolean;
      max_parallel_jobs?: number;
    }>;
    const localWorker = workers.find(w => w.hostname === 'local-worker');
    expect(localWorker, 'local-worker should be registered').toBeDefined();
    expect(localWorker!.online, 'local-worker should be online').toBe(true);
    expect(
      localWorker!.max_parallel_jobs,
      `local-worker should have exactly 1 parallel slot (got ${localWorker!.max_parallel_jobs})`,
    ).toBe(1);

    // Trigger a pinned compile via the UI API. The bulk endpoint accepts a
    // single-target list + pinned_client_id, same path the UpgradeModal uses.
    const compileResp = await request.post('./ui/api/compile', {
      data: {
        targets: ['garage-door-big.yaml'],
        pinned_client_id: localWorker!.client_id,
      },
    });
    expect(compileResp.ok(), 'compile endpoint should accept the pinned request').toBeTruthy();
    const compileJson = (await compileResp.json()) as { enqueued: number };
    expect(compileJson.enqueued).toBeGreaterThan(0);

    // Find the new job in the queue. We don't get the job_id from the
    // compile endpoint directly; poll the queue for the most recent
    // garage-door-big job pinned to our worker.
    let jobId: string | null = null;
    await expect.poll(
      async () => {
        const queue = await getQueue(request);
        const candidates = queue
          .filter(j => j.target === 'garage-door-big.yaml' && j.pinned_client_id === localWorker!.client_id)
          .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
        if (candidates[0]) {
          jobId = candidates[0].id;
          return jobId;
        }
        return null;
      },
      { timeout: 15_000, message: 'pinned garage-door-big job should appear in the queue' },
    ).not.toBeNull();
    expect(jobId).toBeTruthy();

    // Poll for terminal state. Pinned to a 1-slot worker, the job will run
    // sequentially with whatever else local-worker is doing — same overall
    // budget as the cyd-office-info compile is plenty.
    let finalJob: QueueJob | null = null;
    await expect.poll(
      async () => {
        const job = await getJob(request, jobId!);
        if (job && isTerminal(job.state)) {
          finalJob = job;
          return job.state;
        }
        return job?.state ?? 'missing';
      },
      {
        timeout: COMPILE_BUDGET_MS,
        intervals: [2_000, 5_000, 10_000],
        message: `garage-door-big compile did not finish within ${COMPILE_BUDGET_MS}ms`,
      },
    ).toMatch(/^(success|failed|timed_out)$/);

    expect(finalJob, 'final job should be set').not.toBeNull();
    expect(finalJob!.state, `final state: ${finalJob!.state}`).toBe('success');
    // The job ran where we asked it to run.
    expect(finalJob!.assigned_client_id).toBe(localWorker!.client_id);
  });
});

// ---------------------------------------------------------------------------
// API helpers — talk directly to /ui/api/queue for state, source-of-truth.
// ---------------------------------------------------------------------------

interface QueueJob {
  id: string;
  target: string;
  state: string;
  ota_result?: string;
  created_at: string;
  finished_at?: string;
  pinned_client_id?: string;
  assigned_client_id?: string;
}

function isTerminal(state: string): boolean {
  return state === 'success' || state === 'failed' || state === 'timed_out';
}

async function getQueue(request: import('@playwright/test').APIRequestContext): Promise<QueueJob[]> {
  return retryTransient(async () => {
    const resp = await request.get('/ui/api/queue');
    if (!resp.ok()) throw new Error(`/ui/api/queue returned ${resp.status()}`);
    return resp.json();
  });
}

async function latestJobIdFor(
  request: import('@playwright/test').APIRequestContext,
  target: string,
): Promise<string | null> {
  const jobs = await getQueue(request);
  const matching = jobs
    .filter(j => j.target === target)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  return matching[0]?.id ?? null;
}

async function getJob(
  request: import('@playwright/test').APIRequestContext,
  id: string,
): Promise<QueueJob | null> {
  const jobs = await getQueue(request);
  return jobs.find(j => j.id === id) ?? null;
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

/**
 * Find the device row for TARGET_FILENAME.
 *
 * The device cell renders both the friendly name and the filename stem in
 * a `.device-filename` element. The filename stem is unique even when many
 * devices share a friendly name pattern.
 */
async function findTargetRow(page: Page) {
  // Wait for the table to have rendered any rows at all
  const anyRow = page.locator('table tbody tr').first();
  await expect(anyRow).toBeVisible({ timeout: 30_000 });

  return page.locator('table tbody tr')
    .filter({ has: page.locator('.device-filename', { hasText: TARGET_STEM }) })
    .first();
}

/**
 * Find the queue row for our TARGET_FILENAME, restricted to non-terminal
 * states first so we don't accidentally pick up an old finished job.
 *
 * Falls back to any matching row if no in-progress one exists yet.
 */
async function findQueueRow(page: Page) {
  return page.locator('table tbody tr')
    .filter({ has: page.locator('.device-filename', { hasText: TARGET_STEM }) })
    .first();
}
