import { test as base, expect, type Page } from '@playwright/test';
import { control, state } from './api';
export const test = base.extend<{
    isolation: void;
}>({
    isolation: [async ({ request, page }, use, testInfo) => {
            const consoleLog: string[] = [];
            page.on('console', message => consoleLog.push(`${message.type()}: ${message.text()}`));
            page.on('pageerror', error => consoleLog.push(`pageerror: ${error.message}`));
            await control(request, 'reset');
            await use();
            if (testInfo.status !== testInfo.expectedStatus) {
                if (!page.isClosed())
                    await testInfo.attach('screenshot', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
                await testInfo.attach('application-state', { body: JSON.stringify(await state(request), null, 2), contentType: 'application/json' });
                await testInfo.attach('browser-console', { body: consoleLog.join('\n'), contentType: 'text/plain' });
            }
            await page.close();
        }, { auto: true }],
});
export { expect };
export async function login(page: Page, sessionId?: string) {
    await page.goto(sessionId ? `/?session=${sessionId}` : '/');
    await page.locator('input[type=password]').fill('e2e-access-token');
    await page.getByRole('button', { name: 'Continue', exact: true }).click();
    await expect(page.getByPlaceholder('Message Claude…')).toBeVisible();
}
export async function send(page: Page, text: string) {
    await page.getByPlaceholder('Message Claude…').fill(text);
    const accepted = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/messages'));
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    expect((await accepted).status()).toBe(202);
}
