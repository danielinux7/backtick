import { defineConfig, devices } from '@playwright/test';
import { existsSync } from 'fs';

// E2E smoke test for the chart-replay UI.
// Prereqs (one-time): full backend deps in a venv, e.g.
//   /tmp/backtick_venv/bin/pip install -r requirements.txt
// Browser: `npx playwright install chromium` on supported OSes. On distros
// Playwright has no build for yet (e.g. Ubuntu 26.04), install a system
// Chromium (`sudo snap install chromium`) — it's auto-detected below, or set
// PLAYWRIGHT_CHROMIUM_PATH to a binary explicitly.
// Then:  npx playwright test

const systemChromium = ['/snap/bin/chromium', '/usr/bin/chromium', '/usr/bin/chromium-browser']
  .find((p) => existsSync(p));
const chromiumPath = process.env.PLAYWRIGHT_CHROMIUM_PATH ?? systemChromium;

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: 'http://localhost:8765',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // Fall back to Playwright's bundled chromium when no system one is found.
        // A system (e.g. snap) chromium needs the sandbox disabled to launch.
        ...(chromiumPath
          ? {
              launchOptions: {
                executablePath: chromiumPath,
                args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
              },
              // chromiumSandbox at the project level (not launchOptions)
              chromiumSandbox: false,
            }
          : {}),
      },
    },
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
