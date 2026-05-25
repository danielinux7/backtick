import { test, expect } from '@playwright/test';

// Smoke test: load the default symbol, step a candle, place a long, and
// assert the trade shows up in the trades table. Hits live Binance, so a
// network failure here is flakiness, not a code regression.
test('load SOLUSDT, step, place a long, see it in the trades table', async ({ page }) => {
  await page.goto('/');

  // The page auto-loads SOLUSDT 4h; clicking Load is harmless if already armed.
  // Controls stay disabled until a session is loaded — wait for that signal.
  const longBtn = page.locator('#long-btn');
  const nextBtn = page.locator('#next-1');

  // Ensure a session is loaded (symbol defaults to SOLUSDT in #symbol-input).
  await page.getByRole('button', { name: 'Load' }).click().catch(() => {});
  await expect(longBtn).toBeEnabled({ timeout: 30_000 });
  await expect(nextBtn).toBeEnabled();

  // Step one candle forward.
  await nextBtn.click();

  // Place a market long; it should fill immediately and appear in the table.
  const rowsBefore = await page.locator('#trades-table tbody tr').count();
  await longBtn.click();

  await expect
    .poll(async () => page.locator('#trades-table tbody tr').count(), { timeout: 15_000 })
    .toBeGreaterThan(rowsBefore);

  // The new row should reflect a long position.
  await expect(page.locator('#trades-table tbody')).toContainText(/long/i);
});
