import { defineConfig, devices } from '@playwright/test';

// E2E smoke test for the chart-replay UI.
// Prereqs (one-time): full backend deps in a venv, e.g.
//   /tmp/backtick_venv/bin/pip install -r requirements.txt
// Then:  npx playwright test
// (npx will offer to install @playwright/test + browsers on first run.)

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: 'http://localhost:8765',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  // Auto-starts the app if it isn't already running. Reuses an existing
  // uvicorn on :8765 so you can keep one running during local dev.
  webServer: {
    command: '/tmp/backtick_venv/bin/uvicorn backend.main:app --port 8765',
    url: 'http://localhost:8765',
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
