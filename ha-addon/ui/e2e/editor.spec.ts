import { expect, test } from '@playwright/test';
import { mockApi } from './fixtures';

// PW.6 — Editor modal Playwright tests
//
// The editor opens when the user clicks "Edit" on a device row. It contains
// a Monaco editor with the YAML config, plus Save / Validate / Save & Upgrade
// buttons. Closing with unsaved changes shows a confirm dialog.

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.goto('/');
  // Wait for the device table to populate before doing anything
  await expect(page.getByText('Living Room Sensor')).toBeVisible({ timeout: 5000 });
});

async function openEditor(page: import('@playwright/test').Page) {
  // Each device row has an Edit button. Click the first one (Living Room Sensor).
  // There are multiple "Edit" buttons (one per row) — use the first.
  await page.getByRole('button', { name: /^edit$/i }).first().click();
  // Monaco renders into a div with the .monaco-editor class
  await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 10_000 });
}

test('clicking Edit opens the editor modal with Monaco', async ({ page }) => {
  await openEditor(page);
  // Editor body should contain the fixture YAML content. The .monaco-editor
  // div being visible isn't enough — Monaco can fail to load and leave an
  // empty container. Assert the YAML text is actually rendered. Regression
  // guard for #15 where the CSP from E.9 blocked Monaco's CDN load and the
  // editor showed an empty loading state forever.
  await expect(page.locator('.monaco-editor').first()).toBeVisible();
  // The fixture configContent in fixtures.ts starts with "esphome:".
  // Monaco renders text into spans inside .view-line elements.
  await expect(
    page.locator('.monaco-editor .view-line').first(),
  ).toContainText(/esphome|wifi|api/, { timeout: 10_000 });
});

test('editor modal has Save, Validate, and Save & Upgrade buttons', async ({ page }) => {
  await openEditor(page);
  // Save button (exact match avoids matching "Save & Upgrade")
  await expect(page.getByRole('button', { name: /^save$/i })).toBeVisible();
  // Save & Upgrade
  await expect(page.getByRole('button', { name: /save & upgrade/i })).toBeVisible();
  // Validate
  await expect(page.getByRole('button', { name: /^validate$/i })).toBeVisible();
});

test('clicking Save fires the save API, shows a toast, and keeps the modal open', async ({ page }) => {
  let saveHits = 0;
  let lastBody: { content?: string; commit_message?: string } | null = null;
  await page.route('**/ui/api/targets/*/content', async route => {
    if (route.request().method() === 'POST') {
      saveHits++;
      lastBody = route.request().postDataJSON() as typeof lastBody;
      return route.fulfill({ json: { ok: true } });
    }
    return route.fallback();
  });

  await openEditor(page);
  await page.getByRole('button', { name: /^save$/i }).click();

  // Bug #24: Save now opens a commit-message dialog (auto-commit ON by
  // default per the fixture). Type a message and confirm. The same dialog
  // also handles the blank-message → fallback-subject path.
  const commitDialog = page.getByRole('dialog').filter({ hasText: /commit message for save/i });
  await expect(commitDialog).toBeVisible();
  await commitDialog.getByPlaceholder(/^save:/).fill('tidied wifi block');
  await commitDialog.getByRole('button', { name: /save and commit/i }).click();

  await expect.poll(() => saveHits).toBeGreaterThan(0);
  expect(lastBody?.commit_message).toBe('tidied wifi block');

  // Bug #136: after save the modal must remain open — Close is now explicit.
  await expect(page.locator('.monaco-editor').first()).toBeVisible();
});

test('Ctrl+S in the editor fires the save API (bug #135)', async ({ page }) => {
  let saveHits = 0;
  await page.route('**/ui/api/targets/*/content', async route => {
    if (route.request().method() === 'POST') {
      saveHits++;
      return route.fulfill({ json: { ok: true } });
    }
    return route.fallback();
  });

  await openEditor(page);
  // Click inside the editor so Monaco has keyboard focus before Ctrl+S.
  await page.locator('.monaco-editor').first().click();

  // With auto-commit ON the commit-message dialog opens. Press Ctrl+S and
  // confirm the dialog to complete the save flow.
  const modifier = process.platform === 'darwin' ? 'Meta' : 'Control';
  await page.keyboard.press(`${modifier}+s`);

  // Commit-message dialog should appear (same as clicking Save).
  const commitDialog = page.getByRole('dialog').filter({ hasText: /commit message for save/i });
  await expect(commitDialog).toBeVisible();
  await commitDialog.getByRole('button', { name: /save and commit/i }).click();

  await expect.poll(() => saveHits, { message: 'Ctrl+S should trigger the save API' }).toBeGreaterThan(0);

  // Bug #136: modal stays open after Ctrl+S save.
  await expect(page.locator('.monaco-editor').first()).toBeVisible();
});

test('Close button closes the modal when there are no unsaved changes', async ({ page }) => {
  await openEditor(page);
  // The Close button is always present in the footer (bug #136).
  const closeBtn = page.getByRole('button', { name: /^close$/i });
  await expect(closeBtn).toBeVisible();
  await closeBtn.click();
  await expect(page.locator('.monaco-editor').first()).not.toBeVisible({ timeout: 5000 });
});

test('clicking Validate saves, fires the validate API, and shows a toast', async ({ page }) => {
  // Bug #25: validation runs directly on the server (no queue). The flow:
  // save → POST /ui/api/validate → immediate { success, output } response
  // → success toast.
  let saveHits = 0;
  let validateHits = 0;
  await page.route('**/ui/api/targets/*/content', route => {
    if (route.request().method() === 'POST') {
      saveHits++;
      return route.fulfill({ json: { ok: true } });
    }
    return route.fallback();
  });
  await page.route('**/ui/api/validate', route => {
    validateHits++;
    return route.fulfill({ json: { success: true, output: 'Configuration is valid!' } });
  });

  await openEditor(page);
  await page.getByRole('button', { name: /^validate$/i }).click();

  // Save fires first, then validate endpoint.
  await expect.poll(() => saveHits, { message: 'Validate should save first' }).toBeGreaterThan(0);
  await expect.poll(() => validateHits, { message: 'Validate API should be called' }).toBeGreaterThan(0);

  // Success toast should appear (Sonner toast contains the text).
  await expect(page.getByText(/validation passed/i)).toBeVisible({ timeout: 5_000 });
});

test('clicking Save & Upgrade fires save then opens the Upgrade modal', async ({ page }) => {
  // #18: Save & Upgrade in the editor saves the file and then opens the
  // UpgradeModal (same one as the per-row Upgrade button), so the user can
  // pick a worker and ESPHome version. The compile fires when the user
  // clicks Upgrade inside the modal — not immediately after save.
  let saveHits = 0;
  let compileHits = 0;
  await page.route('**/ui/api/targets/*/content', route => {
    if (route.request().method() === 'POST') {
      saveHits++;
      return route.fulfill({ json: { ok: true } });
    }
    return route.fallback();
  });
  await page.route('**/ui/api/compile', route => {
    compileHits++;
    return route.fulfill({ json: { enqueued: 1 } });
  });

  await openEditor(page);
  await page.getByRole('button', { name: /save & upgrade/i }).click();

  // Bug #24: Save & Upgrade with auto-commit on opens the commit-message
  // dialog first. Confirm with the default (blank) message, then expect
  // save + the Upgrade modal.
  const commitDialog = page.getByRole('dialog').filter({ hasText: /commit message for save & upgrade/i });
  await expect(commitDialog).toBeVisible();
  await commitDialog.getByRole('button', { name: /save, commit & upgrade/i }).click();

  // Save fires after confirming the commit-message dialog.
  await expect.poll(() => saveHits).toBeGreaterThan(0);

  // The Upgrade modal should now be open. Compile has NOT been called yet.
  expect(compileHits, 'compile should not fire until the user confirms in the modal').toBe(0);
  const upgradeDialog = page.getByRole('dialog').filter({ hasText: /upgrade/i }).last();
  await expect(upgradeDialog).toBeVisible();
  await upgradeDialog.getByRole('button', { name: /^upgrade$/i }).click();

  // Now compile fires.
  await expect.poll(() => compileHits).toBeGreaterThan(0);
});

test('editor modal closes via Escape key when clean', async ({ page }) => {
  await openEditor(page);
  await page.keyboard.press('Escape');
  // Modal is gone
  await expect(page.locator('.monaco-editor').first()).not.toBeVisible({ timeout: 5000 });
});
