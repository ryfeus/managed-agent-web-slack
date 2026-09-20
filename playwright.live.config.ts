import { defineConfig } from '@playwright/test';
export default defineConfig({
    testDir: './e2e/live', workers: 1, retries: 0, timeout: 240000,
    expect: { timeout: 120000 }, reporter: 'list',
    use: { baseURL: process.env.E2E_LIVE_BASE_URL, browserName: 'chromium' },
});
