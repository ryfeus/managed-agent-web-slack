import { test, expect, login, send } from './helpers/fixtures';
import { control, state, ready, answerEvents, drain } from './helpers/api';
import { slackTurn, mention, inject } from './helpers/slack';
test('E2E-001 web streams a basic turn', async ({ page, request }) => {
    await control(request, 'agent/script', { prompt: 'hello', events: answerEvents('Hello from Claude'), automatic: false });
    await login(page);
    await send(page, 'hello');
    await ready(request);
    const id = (await state(request)).agent.sessions[0].id;
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    await control(request, 'agent/advance', { session_id: id, count: 2 });
    await expect(page.locator('.assistant-message')).toContainText('Hello from Claude');
    await control(request, 'agent/advance', { session_id: id });
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    expect((await state(request)).agent.sent_messages).toHaveLength(1);
    await expect(page.locator('.transport-error')).toHaveCount(0);
    await drain(request);
});
test('E2E-005 Slack turn wakes an already open browser session', async ({ page, request }) => {
    const id = await slackTurn(request);
    await login(page, id);
    await expect(page.locator('.assistant-message')).toContainText('Hello from Claude');
    await control(request, 'agent/script', { prompt: 'from Slack', events: answerEvents('Cross surface answer'), automatic: false });
    await inject(request, mention('from Slack', { type: 'message', text: 'from Slack', thread_ts: '100.000001', ts: '100.000002' }));
    const delivery = control(request, 'events/drain');
    await ready(request, id, 2);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    await control(request, 'agent/advance', { session_id: id });
    await delivery;
    await expect(page.locator('.assistant-message').last()).toContainText('Cross surface answer');
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    expect((await state(request)).agent.sessions).toHaveLength(1);
});
test('E2E-006 browser turn projects once to its bound Slack thread', async ({ page, request }) => {
    const id = await slackTurn(request);
    await login(page, id);
    await control(request, 'agent/script', { prompt: 'from web', events: answerEvents('Web to Slack'), automatic: false });
    await send(page, 'from web');
    await ready(request, id);
    await control(request, 'agent/advance', { session_id: id });
    await drain(request);
    await expect(page.locator('.assistant-message').last()).toContainText('Web to Slack');
    const s = await state(request);
    expect(s.slack.messages.filter((m: {
        text: string;
    }) => m.text === 'Web to Slack')).toHaveLength(1);
    expect(s.agent.sessions).toHaveLength(1);
});
test('E2E-009 browser interrupt cancels a pending successful turn', async ({ page, request }) => {
    await control(request, 'agent/script', { prompt: 'long task', events: answerEvents('Must never complete'), automatic: false });
    await login(page);
    await send(page, 'long task');
    await ready(request);
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    const s = await state(request);
    expect(s.agent.interrupts).toHaveLength(1);
    await control(request, 'agent/advance', { session_id: s.agent.sessions[0].id });
    expect(JSON.stringify((await state(request)).agent.events)).not.toContain('Must never complete');
});

test('history reconciliation preserves deltas newer than the HTTP snapshot', async ({ page, request }) => {
    await control(request, 'agent/script', { prompt: 'snapshot race', events: answerEvents('Newer streamed text'), automatic: false });
    await login(page);
    await send(page, 'snapshot race');
    await ready(request);
    const id = (await state(request)).agent.sessions[0].id;
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    let captured!: () => void;
    const snapshot = new Promise<void>(resolve => { captured = resolve; });
    await page.route('**/api/sessions/*/events', async route => {
        const response = await route.fetch();
        captured();
        await gate;
        await route.fulfill({ response });
    });
    try {
        await page.evaluate(() => window.dispatchEvent(new Event('focus')));
        await snapshot;
        await control(request, 'agent/advance', { session_id: id, count: 2 });
        await expect(page.locator('.assistant-message')).toContainText('Newer streamed text');
        const reconciled = page.waitForResponse(response => response.url().endsWith(`/api/sessions/${id}/events`));
        release();
        await reconciled;
        // Browser frames provide a render barrier after the intercepted response.
        await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
        await expect(page.locator('.assistant-message')).toContainText('Newer streamed text');
        await control(request, 'agent/advance', { session_id: id });
        await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
        await expect(page.locator('.assistant-message')).toHaveCount(1);
    } finally {
        release();
        await page.unrouteAll({ behavior: 'wait' });
    }
});
