import { test, expect, login, send } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';

test('PROD-WEB-001 static export authenticates and completes an AG-UI turn', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'production bundle', response: 'Static export response' });
    await login(page);
    await send(page, 'production bundle');
    await expect.poll(async () => (await state(request)).database.agent_tasks.length).toBe(1);
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    await expect(page.locator('.assistant-message')).toContainText('Static export response');
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    const current = await state(request);
    expect(current.a2a.create_calls).toBe(1);
});

test('PROD-WEB-002 static export restores A2A history from a thread link', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'existing thread', response: 'Hydrated static response' });
    await login(page);
    await send(page, 'existing thread');
    await expect.poll(async () => (await state(request)).database.agent_tasks.length).toBe(1);
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    await expect(page.locator('.assistant-message')).toContainText('Hydrated static response');
    const threadId = (await state(request)).database.agent_threads[0].thread_id as string;
    await page.goto(`/?thread=${threadId}`);
    await expect(page).toHaveURL(new RegExp(`thread=${threadId}`));
    await expect(page.locator('.assistant-message')).toContainText('Hydrated static response');
    await expect(page.getByRole('button', { name: 'Copy conversation link' })).toBeVisible();
});

test('PROD-WEB-003 static assets and query navigation do not depend on Next development mode', async ({ page }) => {
    const requests: string[] = [];
    page.on('request', request => requests.push(request.url()));
    const response = await page.goto('/?thread=00000000-0000-4000-8000-000000000999');
    expect(response?.status()).toBe(200);
    await expect(page.locator('input[type=password]')).toBeVisible();
    const assets = await page.locator('script[src], link[rel="stylesheet"]').evaluateAll(elements =>
        elements.map(element =>
            new URL(element.getAttribute('src') || element.getAttribute('href') || '', document.baseURI).href,
        ),
    );
    expect(assets.length).toBeGreaterThan(0);
    for (const asset of assets) {
        const assetResponse = await page.request.get(asset);
        expect(assetResponse.ok(), asset).toBeTruthy();
    }
    expect(requests.some(url => url.includes('webpack-hmr'))).toBe(false);
});
