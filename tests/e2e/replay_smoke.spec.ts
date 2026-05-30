import { test, expect } from '@playwright/test';

// Smoke test: load the default symbol, step a candle, place a long, and
// assert the trade shows up in the trades table. Hits live Binance, so a
// network failure here is flakiness, not a code regression.
test('load SOLUSDT, step, place a long, see it in the trades table', async ({ page }) => {
  await page.goto('/');

  const longBtn = page.locator('#long-btn');
  const nextBtn = page.locator('#next-1');

  // Drive a deterministic Replay session: symbol defaults to SOLUSDT, tf to 4h,
  // and the replay date is pre-filled (~60 days back). Selecting Replay mode
  // triggers a single auto-load. (Do NOT also click Load — a second load races
  // with the trade and resetForNewSession would wipe the trades table.)
  // Mode is a custom dropdown (the native <select> is hidden); open it and pick Replay.
  await page.locator('.dd:has(#mode-select) .dd-trigger').click();
  await page.locator('.dd:has(#mode-select) .dd-row', { hasText: 'Replay' }).click();

  // Controls stay disabled until a session is loaded — wait for that signal.
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
