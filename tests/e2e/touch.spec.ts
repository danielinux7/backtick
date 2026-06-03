import { test, expect } from '@playwright/test';

// Touch / mobile E2E. Mirrors the desktop replay smoke but drives the UI with
// real touch taps (no mouse, no :hover) on a phone viewport. It covers the two
// things that only exist / behave differently under touch:
//   - the fixed mobile order bar (#m-buy / #m-sell), the desktop Long/Short
//     buttons being hidden on mobile, and
//   - the tick/candle mode toggle, whose active "teal" highlight must stand on
//     its own with no hover (a desktop :hover rule used to mask it).
// Hits live Binance, so a network failure here is flakiness, not a regression.
test('touch: replay loads, mode toggle highlights, market long via the mobile bar', async ({
  page,
}) => {
  await page.goto('/app');

  // Pick Replay from the segmented Live/Replay toggle — by tap.
  await page.locator('#mode-toggle button[data-mode="replay"]').tap();

  // Session loaded → the Long button un-disables. It's hidden on mobile, but it
  // still tracks session state, so toBeEnabled is a reliable readiness signal.
  await expect(page.locator('#long-btn')).toBeEnabled({ timeout: 30_000 });

  // Tick/candle toggle must highlight purely from .active — there is no hover on
  // touch. The active pill paints a teal gradient (background-image), so assert
  // the gradient is present rather than a flat background-color.
  const candle = page.locator('#speed-mode button[data-mode="candle"]');
  const tick = page.locator('#speed-mode button[data-mode="tick"]');
  await candle.tap();
  await expect(candle).toHaveClass(/\bactive\b/);
  await expect(candle).toHaveCSS('background-image', /linear-gradient/);
  await expect(tick).not.toHaveClass(/\bactive\b/);

  // Step a candle, then tap Buy on the fixed order bar → market long. A plain
  // tap (no drag) routes to mobileMarketOrder via the pointer handlers.
  await page.locator('#next-1').tap();
  const rowsBefore = await page.locator('#trades-table tbody tr').count();
  await page.locator('#m-buy').tap();

  await expect
    .poll(() => page.locator('#trades-table tbody tr').count(), { timeout: 15_000 })
    .toBeGreaterThan(rowsBefore);
  await expect(page.locator('#trades-table tbody')).toContainText(/long/i);
});
