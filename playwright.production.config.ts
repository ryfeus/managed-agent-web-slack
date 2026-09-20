import { defineConfig } from '@playwright/test';

export default defineConfig({
    testDir: './e2e',
    testMatch: 'production-web.spec.ts',
    fullyParallel: false,
    workers: 1,
    retries: 0,
    timeout: 45000,
    expect: { timeout: 15000 },
    outputDir: 'test-results/production-web-playwright',
    reporter: [
        ['list'],
        ['html', { open: 'never', outputFolder: 'playwright-report-production' }],
    ],
    use: {
        baseURL: 'http://127.0.0.1:3000',
        browserName: 'chromium',
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
});
