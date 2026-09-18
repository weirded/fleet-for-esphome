import { expect, test } from '@playwright/test';
import { mockApi } from './fixtures';

// QS.25 follow-up — Workers tab actions: clean cache, parallel-jobs slot
// control, delete offline worker. WL.3 consolidated Clean Cache / Delete
// and "View logs" into a single per-row "Actions" dropdown menu.
//
// Worker lifecycle: workers never leave the list on their own (not on a
// clean shutdown, not on an add-on restart). Delete is the only exit, so
// the item is always present — disabled with a hint while the worker is
// online, since a running worker would just re-register.

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  await expect(page.getByText('Living Room Sensor')).toBeVisible({ timeout: 5000 });
  await page.getByRole('button', { name: /Workers/ }).click();
  await expect(page.getByText('build-server-1').first()).toBeVisible({ timeout: 5000 });
});

function workerRow(page: import('@playwright/test').Page, hostname: string) {
  return page.locator('table tbody tr').filter({ hasText: hostname });
}

async function openActions(page: import('@playwright/test').Page, hostname: string) {
  await workerRow(page, hostname).getByRole('button', { name: new RegExp(`Actions for ${hostname}`) }).click();
}

test('actions dropdown reveals Clean cache for online workers', async ({ page }) => {
  await openActions(page, 'build-server-1');
  await expect(page.getByRole('menuitem', { name: 'Clean cache' })).toBeVisible();
});

test('Delete is present but disabled with a hint for online workers', async ({ page }) => {
  await openActions(page, 'build-server-1');
  const deleteItem = page.getByRole('menuitem', { name: 'Delete' });
  await expect(deleteItem).toBeVisible();
  await expect(deleteItem).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByTitle(/stop the worker first/i)).toBeVisible();
});

test('Delete is present but disabled with a hint for the built-in worker', async ({ page }) => {
  // The shared fixture has no built-in worker (it would ripple through
  // every worker-selection spec), so this test serves its own list.
  const { workers } = await import('./fixtures');
  await page.unroute('**/ui/api/workers');
  await page.route('**/ui/api/workers', route =>
    route.fulfill({
      json: [
        ...workers,
        { ...workers[0], client_id: 'worker-local', hostname: 'local-worker', tags: [] },
      ],
    }),
  );
  await expect(page.getByText('local-worker').first()).toBeVisible({ timeout: 5000 });
  await openActions(page, 'local-worker');
  const deleteItem = page.getByRole('menuitem', { name: 'Delete' });
  await expect(deleteItem).toBeVisible();
  await expect(deleteItem).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByTitle(/built-in worker/i)).toBeVisible();
});

test('actions dropdown enables Delete for offline workers', async ({ page }) => {
  await openActions(page, 'build-server-2');
  const deleteItem = page.getByRole('menuitem', { name: 'Delete' });
  await expect(deleteItem).toBeVisible();
  await expect(deleteItem).not.toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByRole('menuitem', { name: 'Clean cache' })).toHaveCount(0);
});

test('Clean cache fires POST /workers/{id}/clean', async ({ page }) => {
  let cleanedFor: string | null = null;
  // Clear the mockApi fixture's handler for this pattern so ours is
  // the only one that runs (Playwright's route stack doesn't always
  // prefer the newest registration when the patterns match identically).
  await page.unroute('**/ui/api/workers/*/clean');
  await page.route('**/ui/api/workers/*/clean', route => {
    if (route.request().method() === 'POST') {
      const url = route.request().url();
      cleanedFor = url.split('/workers/')[1].split('/')[0];
    }
    route.fulfill({ status: 200, json: {} });
  });

  await openActions(page, 'build-server-1');
  const cleanItem = page.getByRole('menuitem', { name: 'Clean cache' });
  await expect(cleanItem).toBeVisible();
  await cleanItem.click();

  await expect.poll(() => cleanedFor).toBe('worker-1');
});

test('Delete offline worker fires DELETE /workers/{id}', async ({ page }) => {
  let removedFor: string | null = null;
  await page.unroute('**/ui/api/workers/*');
  await page.route('**/ui/api/workers/*', route => {
    if (route.request().method() === 'DELETE') {
      const url = route.request().url();
      removedFor = url.split('/workers/')[1];
    }
    route.fulfill({ status: 200, json: {} });
  });

  await openActions(page, 'build-server-2');
  const removeItem = page.getByRole('menuitem', { name: 'Delete' });
  await expect(removeItem).toBeVisible();
  await removeItem.click();

  await expect.poll(() => removedFor).toBe('worker-2');
});

test('parallel-jobs +/- buttons debounce-fire POST /parallel-jobs', async ({ page }) => {
  let lastPayload: { max_parallel_jobs?: number; id?: string } | null = null;
  await page.route('**/ui/api/workers/*/parallel-jobs', route => {
    if (route.request().method() === 'POST') {
      const url = route.request().url();
      const id = url.split('/workers/')[1].split('/')[0];
      try {
        const body = JSON.parse(route.request().postData() ?? '{}');
        lastPayload = { ...body, id };
      } catch { /* ignore */ }
    }
    route.fulfill({ status: 200, json: {} });
  });

  // worker-1 has max_parallel_jobs=2 in fixtures. Click + once → debounced
  // POST 600ms later with count=3.
  const row = workerRow(page, 'build-server-1');
  // The slot control's "+" button is the second small Button in the slot
  // cell; locate by text content ("+").
  await row.getByRole('button', { name: '+', exact: true }).click();

  await expect.poll(() => lastPayload?.id, { timeout: 3_000 }).toBe('worker-1');
  expect(lastPayload!.max_parallel_jobs).toBe(3);
});

test('Connect Worker button opens the modal', async ({ page }) => {
  await page.getByRole('button', { name: /connect worker/i }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('heading', { name: /Connect a Build Worker/i })).toBeVisible();
});

// #109 regression guard: the Actions menu exposes "Request diagnostics"
// for online workers and clicking it hits the round-trip endpoints
// (request → poll → download). Mocked endpoints here — real py-spy
// path is covered by the e2e-hass-4 suite.
test('Actions dropdown shows Request diagnostics for online workers and fires the round-trip', async ({ page }) => {
  let requestedFor: string | null = null;
  let polledWith: string | null = null;
  await page.route(/\/ui\/api\/workers\/([^/]+)\/request-diagnostics$/, async route => {
    const m = route.request().url().match(/\/workers\/([^/]+)\/request-diagnostics/);
    requestedFor = m ? m[1] : null;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ request_id: 'req-abc' }),
    });
  });
  await page.route(/\/ui\/api\/workers\/([^/]+)\/diagnostics\/req-abc$/, async route => {
    const m = route.request().url().match(/\/workers\/([^/]+)\/diagnostics\/req-abc/);
    polledWith = m ? m[1] : null;
    await route.fulfill({
      status: 200,
      contentType: 'text/plain',
      headers: {
        'X-Diagnostics-Ok': '1',
        'Content-Disposition': 'attachment; filename="worker-diagnostics-build-server-1.txt"',
      },
      body: 'Thread 1 (idle): MainThread\n    select (selectors.py:468)\n',
    });
  });

  await openActions(page, 'build-server-1');
  const item = page.getByRole('menuitem', { name: 'Request diagnostics' });
  await expect(item).toBeVisible();
  await item.click();

  await expect.poll(() => requestedFor, { timeout: 3_000 }).toBe('worker-1');
  await expect.poll(() => polledWith, { timeout: 3_000 }).toBe('worker-1');
});

test('Actions dropdown hides Request diagnostics for offline workers', async ({ page }) => {
  await openActions(page, 'build-server-2');
  await expect(page.getByRole('menuitem', { name: 'Request diagnostics' })).toHaveCount(0);
});

// TR.4 regression guard: the bash + powershell branches must include
// `--network host`, otherwise a user pasting the command onto a LAN
// docker host gets a worker on the default bridge that can't OTA to
// ESP devices. The compose branch already had `network_mode: host`.
test('Connect Worker docker command includes --network host on every format', async ({ page }) => {
  await page.getByRole('button', { name: /connect worker/i }).click();
  const dialog = page.getByRole('dialog');

  // Bash is the default — assert first.
  await expect(dialog.locator('.docker-cmd')).toContainText('--network host');

  // PowerShell branch uses the same `--network host` shape.
  await dialog.getByRole('button', { name: 'PowerShell' }).click();
  await expect(dialog.locator('.docker-cmd')).toContainText('--network host');

  // Docker Compose branch uses `network_mode: host` instead.
  await dialog.getByRole('button', { name: 'Docker Compose' }).click();
  await expect(dialog.locator('.docker-cmd')).toContainText('network_mode: host');
});
