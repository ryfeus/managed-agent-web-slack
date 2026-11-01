import { defineConfig } from '@playwright/test';
export default defineConfig({
    testDir: './e2e',
    testIgnore: ['**/live/**', '**/production-web.spec.ts', '**/helpers/**/*.test.ts'],
    fullyParallel: false,
    workers: 1,
    retries: 0,
    timeout: 45000,
    expect: { timeout: 15000 },
    outputDir: process.env.E2E_ARTIFACT_DIR ? `${process.env.E2E_ARTIFACT_DIR}/playwright` : 'test-results/playwright',
    reporter: [['list'], ['html', { open: 'never', outputFolder: process.env.E2E_ARTIFACT_DIR ? `${process.env.E2E_ARTIFACT_DIR}/report` : 'playwright-report' }]],
    use: {
        baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:3000',
        browserName: 'chromium',
        trace: 'retain-on-failure',
        screenshot: 'only-on-failure',
    },
});
