import { test, expect, login } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { inject, mention, interaction } from './helpers/slack';
import type { APIRequestContext } from '@playwright/test';

async function startReview(request: APIRequestContext) {
    await control(request, 'policy', { fixture: 'human-review' });
    await control(request, 'a2a/script', { prompt: 'review search', events: [
        { id: 'review-call', type: 'agent.tool_use', name: 'web_search', input: { query: 'specific human review' } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['review-call'] } },
    ] });
    await inject(request, mention('review search'));
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('INPUT_REQUIRED');
    const current = await state(request);
    expect(current.a2a.confirm_calls).toBe(0);
    const message = current.slack.messages.find((item: { blocks?: object[] }) => (JSON.stringify(item.blocks) || "").includes('agent_tool_allow'));
    expect(JSON.stringify(message.blocks)).toContain('user-approval');
    expect(JSON.stringify(message.blocks)).toContain('specific human review');
    return { threadId: current.database.agent_threads[0].thread_id as string, row: current.database.cma_input_requests[0], message };
}

for (const ruling of ['allow', 'deny'] as const) {
    test(`native OpenAPPA review survives Web reload and ${ruling} settles Slack controls`, async ({ page, request }) => {
        const pending = await startReview(request);
        await login(page, pending.threadId);
        await expect(page.locator('.interrupt-panel')).toContainText('user-approval');
        await expect(page.locator('.interrupt-panel')).toContainText('specific human review');
        await page.reload();
        await expect(page.getByRole('button', { name: 'Allow', exact: true })).toBeVisible();
        await page.getByRole('button', { name: ruling === 'allow' ? 'Allow' : 'Deny', exact: true }).click();
        await page.getByRole('button', { name: 'Submit responses' }).click();
        await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('COMPLETED');
        await expect(page.locator('.interrupt-panel')).toHaveCount(0);
        const current = await state(request);
        expect(current.database.cma_input_requests[0]).toMatchObject({ human_ruling: ruling, decision: ruling, status: 'resolved' });
        expect(current.a2a.confirm_calls).toBe(1);
        expect(current.slack.messages.find((item: {ts: string}) => item.ts === pending.message.ts).blocks).toEqual([]);
        expect(JSON.stringify(current.database)).not.toContain('specific human review');
    });
}

test('signed Slack review requires ownership, and first ruling removes controls in Web', async ({ page, request }) => {
    const pending = await startReview(request);
    await login(page, pending.threadId);
    await expect(page.getByRole('button', { name: 'Allow', exact: true })).toBeVisible();
    const value = JSON.stringify({ taskId: pending.row.task_id, requestId: pending.row.request_id });
    const action = interaction('agent_tool_allow', pending.message.ts, value);
    await inject(request, { ...action, user: { id: 'U002' } }, true);
    await drain(request);
    expect((await state(request)).a2a.confirm_calls).toBe(0);
    await inject(request, action, true);
    await inject(request, action, true);
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('COMPLETED');
    await expect(page.locator('.interrupt-panel')).toHaveCount(0);
    await expect(page.locator('.assistant-message').last()).toContainText('Done');
    await inject(request, interaction('agent_tool_deny', pending.message.ts, value), true);
    await drain(request);
    const current = await state(request);
    expect(current.database.cma_input_requests[0].decision).toBe('allow');
    expect(current.a2a.confirm_calls).toBe(1);
});
