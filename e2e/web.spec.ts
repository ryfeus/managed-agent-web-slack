import { test, expect, login, send } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { slackTurn } from './helpers/slack';

test('E2E-001 Web sends and renders a completed A2A turn', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'hello', response: 'Hello from Claude' });
    await login(page);
    await send(page, 'hello');
    await expect.poll(async () => (await state(request)).database.agent_tasks.length).toBe(1);
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    await expect(page.locator('.assistant-message')).toContainText('Hello from Claude');
    const current = await state(request);
    expect(current.a2a.create_calls).toBe(1);
    expect(current.a2a.send_calls).toBe(0);
    expect(current.database.agent_tasks).toHaveLength(1);
    expect(current.database.agent_tasks[0].client_message_id).toMatch(/^web:agui:/);
    await expect(page.locator('.transport-error')).toHaveCount(0);
});

test('E2E-005 Slack-created thread restores canonical history in Web', async ({ page, request }) => {
    const threadId = await slackTurn(request, 'from Slack', 'Slack answer');
    await login(page, threadId);
    await expect(page.locator('.user-message')).toContainText('from Slack');
    await expect(page.locator('.assistant-message')).toContainText('Slack answer');
    await expect(page).toHaveURL(new RegExp(`thread=${threadId}`));
});

test('E2E-006 Web turn on Slack-bound thread shares one A2A context', async ({ page, request }) => {
    const threadId = await slackTurn(request, 'from Slack', 'Slack answer');
    await login(page, threadId);
    await control(request, 'a2a/script', { prompt: 'from web', response: 'Web to Slack' });
    await send(page, 'from web');
    await expect.poll(async () => (await state(request)).database.agent_tasks.length).toBe(2);
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.send_calls; }).toBe(1);
    await expect(page.locator('.assistant-message').last()).toContainText('Web to Slack');
    const current = await state(request);
    expect(current.database.agent_threads).toHaveLength(1);
    expect(current.database.agent_tasks).toHaveLength(2);
    expect(current.a2a.sessions).toHaveLength(1);
    expect(current.a2a.send_calls).toBe(1);
    await expect.poll(async () => (await state(request)).slack.messages.some((message: { text: string }) => message.text.includes('Web to Slack'))).toBe(true);
});

test('E2E-009 explicit Stop cancels the A2A Task', async ({ page, request }) => {
    await control(request, 'a2a/automatic', { enabled: false });
    await login(page);
    await send(page, 'long task');
    await expect.poll(async () => (await state(request)).database.agent_tasks.length).toBe(1);
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.interrupt_calls; }).toBe(1);
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
});

test('E2E-010 approval survives reload and resumes the same Task', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'approval task', events: [
        { id: 'tool-approval', type: 'agent.tool_use', name: 'browser', input: { url: 'https://example.com' } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['tool-approval'] } },
    ] });
    await login(page);
    await send(page, 'approval task');
    await expect.poll(async () => (await state(request)).database.agent_tasks.length).toBe(1);
    await expect.poll(async () => {
        await drain(request);
        return (await state(request)).database.cma_tasks[0].a2a_state;
    }).toBe('INPUT_REQUIRED');
    await expect(page.locator('.tool-call')).toContainText('browser');
    await page.reload();
    await expect(page.locator('.tool-call')).toContainText('browser');
    await expect(page.locator('.tool-call pre')).toContainText('https://example.com');
    await page.getByRole('button', { name: 'Allow', exact: true }).click();
    await page.getByRole('button', { name: 'Submit responses' }).click();
    await expect.poll(async () => {
        await drain(request);
        return (await state(request)).a2a.confirm_calls;
    }).toBe(1);
    await expect(page.locator('.assistant-message').last()).toContainText('Done');
    const current = await state(request);
    expect(current.database.agent_tasks).toHaveLength(1);
});

test('E2E-010B queued Task does not hide the earliest pending approval after reload', async ({ page, request }) => {
    await control(request, 'a2a/automatic', { enabled: false });
    await login(page);
    await send(page, 'approval task');
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('WORKING');
    const initial = await state(request);
    const sessionId = initial.a2a.sessions[0].id as string;
    const firstTaskId = initial.database.cma_tasks[0].task_id as string;
    const threadId = new URL(page.url()).searchParams.get('thread');
    expect(threadId).toBeTruthy();
    await page.evaluate((id) => {
        const controller = new AbortController();
        (window as typeof window & { abortQueuedTask?: () => void }).abortQueuedTask = () => controller.abort();
        void fetch('http://127.0.0.1:3001/api/agui', {
            method: 'POST', credentials: 'include', signal: controller.signal,
            headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
            body: JSON.stringify({
                threadId: id, runId: crypto.randomUUID(), state: {}, tools: [], context: [], forwardedProps: {},
                messages: [{ id: crypto.randomUUID(), role: 'user', content: 'queued task' }],
            }),
        }).catch(() => undefined);
    }, threadId);
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks.map((item: { a2a_state: string }) => item.a2a_state).sort(); }).toEqual(['SUBMITTED', 'WORKING']);
    await page.evaluate(() => (window as typeof window & { abortQueuedTask?: () => void }).abortQueuedTask?.());
    await control(request, 'a2a/emit', { session_id: sessionId, event: {
        id: 'tool-approval', type: 'agent.tool_use', name: 'browser', input: { url: 'https://example.com' },
    } });
    await control(request, 'a2a/emit', { session_id: sessionId, event: {
        type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['tool-approval'] },
    } });
    await control(request, 'a2a/reconcile', { session_id: sessionId });
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks.find((item: { task_id: string }) => item.task_id === firstTaskId)?.a2a_state; }).toBe('INPUT_REQUIRED');
    await page.reload();
    await expect(page.locator('.tool-call')).toContainText('browser');
    await page.getByRole('button', { name: 'Allow', exact: true }).click();
    await page.getByRole('button', { name: 'Submit responses' }).click();
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.confirm_calls; }).toBe(1);
    const current = await state(request);
    expect(current.database.agent_tasks).toHaveLength(2);
    expect(current.database.cma_input_requests[0].task_id).toBe(firstTaskId);
});

test('E2E-010C Stop cancels a pending approval and restores Send', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'stop approval', events: [
        { id: 'tool-stop', type: 'agent.tool_use', name: 'browser', input: { url: 'https://example.com' } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['tool-stop'] } },
    ] });
    await login(page);
    await send(page, 'stop approval');
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('INPUT_REQUIRED');
    await expect(page.locator('.tool-call')).toHaveCount(1);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('CANCELED');
    await expect(page.locator('.tool-call')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.locator('.tool-call')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    expect((await state(request)).database.agent_tasks).toHaveLength(1);
});

test('E2E-011 reload during WORKING reconnects without another Task', async ({ page, request }) => {
    await control(request, 'a2a/automatic', { enabled: false });
    await login(page);
    await send(page, 'slow turn');
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    const before = await state(request);
    const sessionId = before.a2a.sessions[0].id as string;
    await page.reload();
    await control(request, 'a2a/emit', { session_id: sessionId, event: {
        type: 'agent.message', content: [{ type: 'text', text: 'Recovered answer' }],
    } });
    await control(request, 'a2a/emit', { session_id: sessionId, event: {
        type: 'session.status_idle', stop_reason: { type: 'end_turn' },
    } });
    await control(request, 'a2a/reconcile', { session_id: sessionId });
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0].a2a_state; }).toBe('COMPLETED');
    await expect(page.locator('.assistant-message').last()).toContainText('Recovered answer');
    const after = await state(request);
    expect(after.database.agent_tasks).toHaveLength(1);
    expect(after.a2a.interrupt_calls).toBe(0);
});

test('E2E-011B history-resume race restores the intervening message once', async ({ page, request }) => {
    await control(request, 'a2a/automatic', { enabled: false });
    await login(page);
    await send(page, 'race turn');
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    const sessionId = (await state(request)).a2a.sessions[0].id as string;
    let releaseResume!: () => void;
    let intercepted!: () => void;
    const held = new Promise<void>((resolve) => { releaseResume = resolve; });
    const observed = new Promise<void>((resolve) => { intercepted = resolve; });
    await page.route('**/api/agui/threads/*/resume', async (route) => {
        intercepted();
        await held;
        await route.continue();
    });
    const reload = page.reload();
    await observed;
    await control(request, 'a2a/emit', { session_id: sessionId, event: {
        type: 'agent.message', content: [{ type: 'text', text: 'Between history and resume' }],
    } });
    await control(request, 'a2a/reconcile', { session_id: sessionId });
    releaseResume();
    await reload;
    await expect(page.locator('.assistant-message').filter({ hasText: 'Between history and resume' })).toHaveCount(1);
    await control(request, 'a2a/emit', { session_id: sessionId, event: {
        type: 'session.status_idle', stop_reason: { type: 'end_turn' },
    } });
    await control(request, 'a2a/reconcile', { session_id: sessionId });
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0].a2a_state; }).toBe('COMPLETED');
    await expect(page.locator('.assistant-message').filter({ hasText: 'Between history and resume' })).toHaveCount(1);
    expect((await state(request)).database.agent_tasks).toHaveLength(1);
});

test('E2E-012 clarification resumes through generic human input', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'clarify task', events: [
        { id: 'ask-one', type: 'agent.custom_tool_use', name: 'ask_user', input: {
            prompt: 'Which environment?', choices: ['staging', 'production'],
        } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['ask-one'] } },
    ] });
    await login(page);
    await send(page, 'clarify task');
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('INPUT_REQUIRED');
    await expect(page.locator('.interrupt-panel')).toContainText('Which environment?');
    await page.getByRole('textbox', { name: 'Clarification answer' }).fill('staging');
    await page.getByRole('button', { name: 'Submit responses' }).click();
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.custom_result_calls; }).toBe(1);
    await expect(page.locator('.assistant-message').last()).toContainText('Done');
    const current = await state(request);
    expect(current.database.cma_clarification_requests).toHaveLength(1);
    expect(JSON.stringify(current.database.cma_clarification_requests)).not.toContain('staging');
});

test('E2E-013 multiple approvals require explicit responses for every request', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'two tools', events: [
        { id: 'tool-one', type: 'agent.tool_use', name: 'browser', input: { url: 'https://example.com' } },
        { id: 'tool-two', type: 'agent.tool_use', name: 'bash', input: { command: 'pwd' } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['tool-one', 'tool-two'] } },
    ] });
    await login(page);
    await send(page, 'two tools');
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('INPUT_REQUIRED');
    await expect(page.locator('.tool-call')).toHaveCount(2);
    await expect(page.getByRole('button', { name: 'Submit responses' })).toBeDisabled();
    await page.getByRole('button', { name: 'Allow', exact: true }).first().click();
    await expect(page.getByRole('button', { name: 'Submit responses' })).toBeDisabled();
    await page.getByRole('button', { name: 'Deny', exact: true }).last().click();
    await page.getByRole('button', { name: 'Submit responses' }).click();
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.confirm_calls; }).toBe(2);
    expect((await state(request)).database.agent_tasks).toHaveLength(1);
});

test('E2E-014 retrying one AG-UI run ID admits one durable turn', async ({ page, request }) => {
    await control(request, 'a2a/script', { prompt: 'retry run', response: 'One answer' });
    await login(page);
    const threadId = new URL(page.url()).searchParams.get('thread');
    expect(threadId).toBeTruthy();
    const runId = crypto.randomUUID();
    const payload = {
        threadId, runId, state: {}, tools: [], context: [], forwardedProps: {},
        messages: [{ id: crypto.randomUUID(), role: 'user', content: 'retry run' }],
    };
    const first = page.context().request.post('http://127.0.0.1:3001/api/agui', {
        data: payload, headers: { accept: 'text/event-stream' }, timeout: 30000,
    });
    await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
    expect((await first).status()).toBe(200);
    const second = page.context().request.post('http://127.0.0.1:3001/api/agui', {
        data: payload, headers: { accept: 'text/event-stream' }, timeout: 30000,
    });
    expect((await second).status()).toBe(200);
    const current = await state(request);
    expect(current.database.agent_tasks).toHaveLength(1);
    expect(current.database.cma_tasks).toHaveLength(1);
    expect(current.a2a.create_calls).toBe(1);
});
