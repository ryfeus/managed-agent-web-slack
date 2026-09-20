import { test, expect, login, send } from './helpers/fixtures';
import { control, state, ready, answerEvents, drain } from './helpers/api';
import { slackTurn } from './helpers/slack';

test('PROD-WEB-001 static export authenticates and completes a streamed turn', async ({ page, request }) => {
    await control(request, 'agent/script', {
        prompt: 'production bundle',
        events: answerEvents('Static export response'),
        automatic: false,
    });
    await login(page);
    await send(page, 'production bundle');
    await ready(request);
    const id = (await state(request)).agent.sessions[0].id as string;
    await control(request, 'agent/advance', { session_id: id });
    await expect(page.locator('.assistant-message')).toContainText('Static export response');
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    await drain(request);
});

test('PROD-WEB-002 static export hydrates an existing cross-surface session', async ({ page, request }) => {
    const id = await slackTurn(request, 'existing session', 'Hydrated static response');
    await login(page, id);
    await expect(page).toHaveURL(new RegExp(`\\?session=${id}$`));
    await expect(page.locator('.assistant-message')).toContainText('Hydrated static response');
    await expect(page.getByRole('button', { name: 'Copy session link' })).toBeVisible();
});

test('PROD-WEB-003 static assets and query navigation do not depend on Next development mode', async ({ page }) => {
    const requests: string[] = [];
    page.on('request', request => requests.push(request.url()));
    const response = await page.goto('/?session=sesn_missing');
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
