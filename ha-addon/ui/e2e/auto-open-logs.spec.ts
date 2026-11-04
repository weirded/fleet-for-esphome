import { expect, test, type Page } from '@playwright/test';
import { mockApi, queue as fixtureQueue, targets as fixtureTargets } from './fixtures';
import type { Job } from '../src/types';

// #137 — automatically open next-step logs.
//
//   1. A single-device Upgrade opens the build log for the job it just
//      enqueued (matched by the compile response's run_id). Bulk upgrades
//      keep the plain Queue-tab landing.
//   2. While that log is open, a successful compile + OTA hands over to
//      the device's Live Logs. Failed and download-only jobs don't.

const RUN_ID = 'run-137';

/**
 * Route /ui/api/compile to return a run_id and /ui/api/queue to append
 * a job of that run whose state the test drives via `setJob`.
 */
async function mockRun(page: Page, initial: Partial<Job> = {}) {
  let job: Job | null = null;
  const base: Job = {
    id: 'job-137',
    target: 'living-room.yaml',
    state: 'pending',
    run_id: RUN_ID,
    created_at: new Date().toISOString(),
    ...initial,
  };
  await page.route('**/ui/api/compile', route => {
    job = { ...base };
    return route.fulfill({ json: { enqueued: 1, run_id: RUN_ID } });
  });
  await page.route('**/ui/api/queue', route =>
    route.fulfill({ json: job ? [...fixtureQueue, job] : fixtureQueue }),
  );
  return {
    setJob(patch: Partial<Job>) {
      job = { ...(job ?? base), ...patch };
    },
  };
}

async function upgradeLivingRoom(page: Page, action: 'upgrade' | 'download' = 'upgrade') {
  const row = page.locator('#tab-devices tbody tr').filter({ hasText: 'Living Room Sensor' });
  await row.getByRole('button', { name: 'Upgrade' }).click();
  const dialog = page.getByRole('dialog');
  if (action === 'download') {
    await dialog.getByRole('radio', { name: /Download Now/ }).check();
    await dialog.getByRole('button', { name: /^Compile & Download$/ }).click();
  } else {
    await dialog.getByRole('button', { name: /^Upgrade$/ }).click();
  }
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test('single-device Upgrade opens the build log, then live logs after OTA success', async ({ page }) => {
  const run = await mockRun(page);
  await page.goto('/');
  await expect(page.getByText('Living Room Sensor')).toBeVisible({ timeout: 5000 });

  await upgradeLivingRoom(page);

  const logDialog = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'living-room.yaml' }) });
  await expect(logDialog).toBeVisible();
  await expect(logDialog.getByText('Pending')).toBeVisible();

  run.setJob({ state: 'working', status_text: 'Compiling' });
  await expect(logDialog.getByText('Compiling', { exact: true })).toBeVisible();

  run.setJob({ state: 'success', ota_result: 'success', duration_seconds: 42 });
  const liveDialog = page.getByRole('dialog').filter({ hasText: 'Live Logs' });
  await expect(liveDialog).toBeVisible();
  await expect(liveDialog.getByRole('heading', { name: 'living-room' })).toBeVisible();
  await expect(logDialog).toHaveCount(0);
});

test('failed job keeps the build log open and does not switch to live logs', async ({ page }) => {
  const run = await mockRun(page);
  await page.goto('/');
  await expect(page.getByText('Living Room Sensor')).toBeVisible({ timeout: 5000 });

  await upgradeLivingRoom(page);
  const logDialog = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'living-room.yaml' }) });
  await expect(logDialog.getByText('Pending')).toBeVisible();

  run.setJob({ state: 'failed', duration_seconds: 10 });
  await expect(logDialog.getByText('Failed', { exact: true })).toBeVisible();
  await expect(page.getByText('Live Logs')).toHaveCount(0);
});

test('download-only job opens the build log but never switches to live logs', async ({ page }) => {
  const run = await mockRun(page, { download_only: true });
  await page.goto('/');
  await expect(page.getByText('Living Room Sensor')).toBeVisible({ timeout: 5000 });

  await upgradeLivingRoom(page, 'download');
  const logDialog = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: 'living-room.yaml' }) });
  await expect(logDialog).toBeVisible();

  run.setJob({ state: 'success', has_firmware: true, duration_seconds: 30 });
  await expect(logDialog.getByText('Ready', { exact: true })).toBeVisible();
  await expect(page.getByText('Live Logs')).toHaveCount(0);
});

test('bulk Upgrade lands on the Queue tab without opening a log', async ({ page }) => {
  let compiled = false;
  await page.route('**/ui/api/compile', route => {
    compiled = true;
    return route.fulfill({ json: { enqueued: fixtureTargets.length, run_id: RUN_ID } });
  });
  await page.route('**/ui/api/queue', route =>
    route.fulfill({
      json: compiled
        ? [...fixtureQueue, ...fixtureTargets.map((t, i): Job => ({
            id: `bulk-${i}`,
            target: t.target,
            state: 'pending',
            run_id: RUN_ID,
            created_at: new Date().toISOString(),
          }))]
        : fixtureQueue,
    }),
  );
  await page.goto('/');
  await expect(page.getByText('Living Room Sensor')).toBeVisible({ timeout: 5000 });

  await page.locator('#tab-devices .actions').getByRole('button', { name: /^Upgrade/ }).click();
  await page.getByRole('menuitem', { name: /^Upgrade All$/ }).click();
  await page.getByRole('dialog').getByRole('button', { name: /^Upgrade/ }).click();

  await expect.poll(() => compiled).toBe(true);
  // Wait until the post-compile queue refresh has rendered the new jobs
  // (the auto-open decision runs right after that refresh).
  await expect(page.locator('#tab-queue tbody tr')).toHaveCount(fixtureQueue.length + fixtureTargets.length);
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
