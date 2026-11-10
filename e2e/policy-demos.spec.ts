import { test, expect, login, send } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { inject, mention, interaction } from './helpers/slack';
import type { APIRequestContext } from '@playwright/test';

async function script(request: APIRequestContext, id: string, name: string, input: object = {}) {
    await control(request, 'a2a/script', { prompt: id, events: [
        { id, type: 'agent.custom_tool_use', name, input },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: [id] } },
    ] });
}
async function settled(request: APIRequestContext, id: string) {
    await expect.poll(async () => {
        await drain(request);
        return (await state(request)).database.cma_input_requests.find((r: any) => r.cma_event_id === id)?.status;
    }).toBe('resolved');
}
async function reviewing(request: APIRequestContext, id: string) {
    await expect.poll(async () => {
        await drain(request);
        return (await state(request)).database.cma_input_requests.find((r: any) => r.cma_event_id === id)?.review_digest;
    }).toBeTruthy();
}
function result(current: any, id: string): any {
    return (Object.values(current.a2a.events).flat() as any[]).find(e => e.type === 'user.custom_tool_result' && e.custom_tool_use_id === id);
}

test('demo starters prefill without starting a Task', async ({ page, request }) => {
    await login(page);
    for (const label of ['Human publication review', 'Accumulated restrictions', 'Redact before publishing']) {
        await page.getByRole('button', { name: label, exact: true }).click();
        await expect(page.getByPlaceholder('Message Claude…')).toHaveValue(/demo_/);
        expect((await state(request)).database.cma_tasks).toHaveLength(0);
    }
});

test('private demo review survives reload and accumulated restrictions block public publication', async ({ page, request }) => {
    await script(request, 'private-read', 'demo_read_private_report');
    await login(page);
    await send(page, 'private-read');
    await reviewing(request, 'private-read');
    await expect(page.getByRole('button', { name: 'Allow', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.locator('.interrupt-panel')).toContainText('demo_read_private_report');
    await page.getByRole('button', { name: 'Allow', exact: true }).click();
    await page.getByRole('button', { name: 'Submit responses' }).click();
    await settled(request, 'private-read');
    for (const [id, name, input] of [
        ['prepare', 'demo_prepare_release', {}],
        ['blocked-public', 'demo_publish_report', { audience: 'public', text: 'synthetic' }],
        ['blocked-redaction', 'demo_read_contact_report', {}],
    ] as const) {
        await script(request, id, name, input);
        await send(page, id);
        await settled(request, id);
    }
    const current = await state(request);
    for (const id of ['blocked-public', 'blocked-redaction']) {
        expect(result(current, id)).toMatchObject({ is_error: true, content: [{ type: 'text', text: 'Denied by OpenAPPA policy.' }] });
        expect(current.database.cma_tool_executions.find((r: any) => r.cma_event_id === id).execution_attempted_at).toBeNull();
    }
    expect(JSON.stringify(current.database)).not.toContain('alex@example.com');
});

test('redacted demo result stays private through signed Slack publication and repeat-effect review', async ({ page, request }) => {
    await script(request, 'redact', 'demo_read_contact_report');
    await inject(request, mention('redact'));
    await settled(request, 'redact');
    const read = await state(request);
    const text = result(read, 'redact').content[0].text;
    expect(text).toContain('[redacted-email]');
    expect(text).not.toContain('alex@example.com');
    await login(page, read.database.agent_threads[0].thread_id);
    await script(request, 'prepare', 'demo_prepare_release');
    await send(page, 'prepare');
    await settled(request, 'prepare');
    for (const [id, ruling] of [['publish', 'allow'], ['repeat', 'deny']] as const) {
        await script(request, id, 'demo_publish_report', { audience: 'public', text });
        await send(page, id);
        await reviewing(request, id);
        await expect(page.getByRole('button', { name: 'Allow', exact: true })).toBeVisible();
        if (id === 'repeat') await expect(page.locator('.interrupt-panel')).toContainText('demo.report_published');
        await expect.poll(async () => {
            await drain(request);
            const current = await state(request);
            const row = current.database.cma_input_requests.find((r: any) => r.cma_event_id === id);
            return current.slack.messages.some((m: any) => JSON.stringify(m.blocks)?.includes(row?.request_id));
        }).toBe(true);
        const current = await state(request);
        const row = current.database.cma_input_requests.find((r: any) => r.cma_event_id === id);
        const card = current.slack.messages.find((m: any) => JSON.stringify(m.blocks)?.includes(row.request_id));
        const action = interaction(`agent_tool_${ruling}`, card.ts, JSON.stringify({ taskId: row.task_id, requestId: row.request_id }));
        await inject(request, action, true);
        await inject(request, action, true);
        await settled(request, id);
        await expect(page.locator('.interrupt-panel')).toHaveCount(0);
    }
    const after = await state(request);
    expect(result(after, 'publish').is_error).toBe(false);
    expect(result(after, 'repeat').is_error).toBe(true);
    expect(after.a2a.confirm_calls).toBe(0);
    expect(JSON.stringify(after.database)).not.toContain('alex@example.com');
    expect(JSON.stringify(after.slack.messages)).not.toContain('alex@example.com');
    expect(await page.locator('body').innerText()).not.toContain('alex@example.com');
});
