import { expect, test, type Page } from '@playwright/test';
import type { AppSettings } from '../src/api/client';
import type { AccentColor } from '../src/lib/accent';
import { mockApi } from './fixtures';

// #150 — accent-colour presets in Settings → Display.

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

function rootVar(page: Page, name: string): Promise<string> {
  return page.evaluate(
    n => getComputedStyle(document.documentElement).getPropertyValue(n).trim().toLowerCase(),
    name,
  );
}

test('picking an accent swatch PATCHes the setting and re-themes the root CSS variables', async ({ page }) => {
  await page.goto('/');
  // Default (dark theme, indigo) — the pre-#150 accent.
  await expect.poll(() => rootVar(page, '--accent')).toBe('#6366f1');

  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const picker = page.getByRole('group', { name: 'Accent colour' });
  await expect(picker.getByRole('button', { name: 'Indigo (default)' })).toHaveAttribute('aria-pressed', 'true');

  const patchRequest = page.waitForRequest(req =>
    req.url().includes('/ui/api/settings') && req.method() === 'PATCH',
  );
  await picker.getByRole('button', { name: 'Teal' }).click();
  const body = JSON.parse((await patchRequest).postData() ?? '{}') as Partial<AppSettings>;
  const expected: AccentColor = 'teal';
  expect(body).toEqual({ accent_color: expected });

  await expect(page.locator('html')).toHaveAttribute('data-accent', 'teal');
  // Dark-theme teal for accent text and the focus ring; --primary (button
  // background) is pinned to the darker teal so white button labels keep
  // >= 4.5:1 contrast — the bright #0d9488 only reaches 3.7:1.
  await expect.poll(() => rootVar(page, '--accent')).toBe('#0d9488');
  await expect.poll(() => rootVar(page, '--primary')).toBe('#0f766e');
  await expect.poll(() => rootVar(page, '--ring')).toBe('#0d9488');
  await expect(picker.getByRole('button', { name: 'Teal' })).toHaveAttribute('aria-pressed', 'true');
  await expect(picker.getByRole('button', { name: 'Indigo (default)' })).toHaveAttribute('aria-pressed', 'false');
});

test('accent persists across reload and uses the light-theme shade in light mode', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('group', { name: 'Accent colour' }).getByRole('button', { name: 'Green' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'green');
  await page.keyboard.press('Escape');

  // Switch to light theme, then reload — server-side setting (mock
  // state) + cached pre-paint value both say green.
  await page.locator('header button[title*="Switch to"]').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-accent', 'green');
  await expect.poll(() => rootVar(page, '--accent')).toBe('#15803d');
});
