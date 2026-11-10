import { test, expect, login, send } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { inject, mention, interaction } from './helpers/slack';
import type { APIRequestContext } from '@playwright/test';

async function script(request: APIRequestContext) {
    await control(request, 'a2a/script', { prompt: 'custom metrics', events: [
        { id: 'custom-metrics', type: 'agent.custom_tool_use', name: 'text_metrics', input: { text: 'private-metrics é' } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['custom-metrics'] } },
    ] });
}

test('custom tool executes through Web and remains canonical after reload', async ({ page, request }) => {
    await script(request);
    await login(page);
    await send(page, 'custom metrics');
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('COMPLETED');
    await page.reload();
    await expect(page.locator('.assistant-message').last()).toContainText('Done');
    const current = await state(request);
    expect(current.a2a.confirm_calls).toBe(0);
    expect(current.a2a.custom_result_calls).toBe(1);
    expect(current.database.cma_tool_executions[0]).toMatchObject({ state: 'resolved' });
    expect(current.database.cma_contexts[0].policy_checkpoint_revision).toBe(2);
    expect(current.database.cma_contexts[0].policy_checkpoint_key).toMatch(/^[a-f0-9]{32}-[a-f0-9]{64}$/);
    expect(JSON.stringify(current.database)).not.toContain('private-metrics');
    expect(await page.locator('body').innerText()).not.toContain('private-metrics');
});

for (const ruling of ['allow', 'deny'] as const) {
    test(`custom tool native review accepts signed Slack ${ruling} and retires Web controls`, async ({ page, request }) => {
        await control(request, 'policy', { fixture: 'human-review' });
        await script(request);
        await inject(request, mention('custom metrics'));
        await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('INPUT_REQUIRED');
        const pending = await state(request);
        const row = pending.database.cma_input_requests[0];
        const card = pending.slack.messages.find((item: { blocks?: object[] }) => JSON.stringify(item.blocks)?.includes('agent_tool_allow'));
        await login(page, pending.database.agent_threads[0].thread_id);
        await page.reload();
        await expect(page.locator('.interrupt-panel')).toContainText('text_metrics');
        await expect(page.locator('.interrupt-panel')).toContainText('private-metrics');
        const action = interaction(`agent_tool_${ruling}`, card.ts, JSON.stringify({ taskId: row.task_id, requestId: row.request_id }));
        await inject(request, action, true);
        await inject(request, action, true);
        await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('COMPLETED');
        await expect(page.locator('.interrupt-panel')).toHaveCount(0);
        const current = await state(request);
        expect(current.a2a.confirm_calls).toBe(0);
        expect(current.a2a.custom_result_calls).toBe(1);
        expect(current.database.cma_contexts[0].policy_checkpoint_revision).toBe(ruling === 'allow' ? 3 : 2);
        expect(current.database.cma_tool_executions[0].execution_attempted_at !== null).toBe(ruling === 'allow');
        expect(current.slack.messages.find((item: { ts: string }) => item.ts === card.ts).blocks).toEqual([]);
    });
}

test('custom tool result withheld by native policy stays private and Stop remains available', async ({ page, request }) => {
    await control(request, 'policy', { fixture: 'custom-result-withheld' });
    await script(request);
    await inject(request, mention('custom metrics'));
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tool_executions[0]?.state; }).toBe('withheld');
    const held = await state(request);
    expect(held.a2a.custom_result_calls).toBe(0);
    expect(held.database.cma_contexts[0].policy_checkpoint_revision).toBe(2);
    expect(held.a2a.confirm_calls).toBe(0);
    expect(JSON.stringify(held.slack.messages)).not.toContain('utf8_bytes');
    expect(JSON.stringify(held.database)).not.toContain('utf8_bytes');
    await login(page, held.database.agent_threads[0].thread_id);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    expect(await page.locator('body').innerText()).not.toContain('utf8_bytes');
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('CANCELED');
    expect((await state(request)).a2a.custom_result_calls).toBe(0);
});
