import { createHmac, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { createLiveSlackClient } from '../helpers/live-slack';
// Bot posts anchor signed synthetic human events. test:live proves actual Slack delivery via APIs.
test('live Slack and Web share A2A Tasks, automatic policy, and stop', async ({ page, request }) => {
    const env = process.env;
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const team = env.E2E_LIVE_SLACK_TEAM_ID!;
    const user = env.E2E_LIVE_SLACK_USER_ID!;
    function inspect(threadTs: string): any {
        return JSON.parse(execFileSync('uv', [
            'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
            '--team-id', team, '--channel-id', channel, '--thread-ts', threadTs,
        ], { encoding: 'utf8', timeout: 30000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } }));
    }
    const slack = createLiveSlackClient(request, env);
    async function signed(payload: object, interaction = false) {
        const raw = interaction ? new URLSearchParams({ payload: JSON.stringify(payload) }).toString() : JSON.stringify(payload);
        const ts = Math.floor(Date.now() / 1000).toString();
        const sig = createHmac('sha256', env.SLACK_SIGNING_SECRET!).update(`v0:${ts}:${raw}`).digest('hex');
        const response = await request.post(`${base}/slack/${interaction ? 'interactions' : 'events'}`, { data: raw, headers: { 'content-type': interaction ? 'application/x-www-form-urlencoded' : 'application/json', 'x-slack-request-timestamp': ts, 'x-slack-signature': `v0=${sig}` } });
        expect(response.ok()).toBeTruthy();
    }
    const auth = await slack('auth.test', {});
    const run = randomUUID();
    const root = await slack('chat.postMessage', { channel, text: `Local E2E live sandbox ${run}` });
    const thread = root.ts;
    async function input(text: string, first = false) {
        const receipt = first ? root : await slack('chat.postMessage', { channel, thread_ts: thread, text: `E2E input: ${text}` });
        const event = { type: 'event_callback', team_id: team, event_id: `Ev_${randomUUID()}`, event: { type: first ? 'app_mention' : 'message', user, channel, ts: receipt.ts, thread_ts: thread, text: first ? `<@${auth.user_id}> ${text}` : text } };
        await signed(event);
        return event;
    }
    async function replies(): Promise<any[]> { return (await slack('conversations.replies', { channel, ts: thread, limit: 100 })).messages; }
    async function answer(marker: string) {
        await expect.poll(async () => (await replies()).some(m => m.ts !== root.ts && !m.text?.startsWith('E2E input:') && JSON.stringify(m).includes(marker)), { intervals: [1000, 2000, 5000] }).toBe(true);
    }
    async function responseCount(marker: string): Promise<number> {
        return (await replies()).filter(m => m.ts !== root.ts && !m.text?.startsWith('E2E input:') && JSON.stringify(m).includes(marker)).length;
    }
    await test.step('Slack app mention', async () => {
        const event = await input(`Reply exactly FIRST_${run}.`, true);
        await answer(`FIRST_${run}`);
        await signed(event);
        await expect.poll(() => inspect(thread).tasks.length).toBe(1);
        expect(await responseCount(`FIRST_${run}`)).toBe(1);
        const state = inspect(thread);
        expect(state.binding.agent_id).toBe('cma');
        expect(state.binding.thread_id).toMatch(/^[0-9a-f-]{36}$/);
        expect(state.binding.context_id).toBeTruthy();
        expect(state.legacy_tables_present).toEqual([]);
        expect(state.tasks[0].client_message_id).toContain(event.event_id);
        await expect.poll(() => inspect(thread).tasks[0].push_delivered).toBe(true);
    });
    await test.step('bound-thread continuation', async () => {
        const threadId = inspect(thread).binding.thread_id;
        await input(`Reply exactly SECOND_${run}.`);
        await answer(`SECOND_${run}`);
        await expect.poll(() => inspect(thread).tasks.length).toBe(2);
        expect(await responseCount(`SECOND_${run}`)).toBe(1);
        expect(inspect(thread).binding.thread_id).toBe(threadId);
    });
    await test.step('OpenAPPA automatically allows the governed Slack tool', async () => {
        const count = inspect(thread).tasks.length;
        await input(`Use web_fetch exactly once to fetch https://example.com and reply with TOOL_${run}.`);
        await expect.poll(() => inspect(thread).tasks.length).toBe(count + 1);
        const taskId = inspect(thread).tasks.at(-1).task_id;
        await expect.poll(() => inspect(thread).tasks.find((t: any) => t.task_id === taskId)?.controller_state,
            { intervals: [1000, 2000, 5000] }).toBe('COMPLETED');
        expect(inspect(thread).tasks.at(-1).input_requests).toEqual([
            expect.objectContaining({decision: 'allow', status: 'resolved'}),
        ]);
        await answer(`TOOL_${run}`);
        expect(JSON.stringify(await replies())).not.toContain('agent_tool_allow');
    });
    await test.step('Stop cancels the active WORKING A2A Task', async () => {
        const count = inspect(thread).tasks.length;
        const completedTasks = inspect(thread).tasks.filter((t: any) => t.controller_state === 'COMPLETED');
        await input('Write a detailed 10000-word tutorial on distributed systems, covering each component in depth.');
        await expect.poll(() => inspect(thread).tasks.length).toBe(count + 1);
        const taskId = inspect(thread).tasks.at(-1).task_id;
        await expect.poll(() => inspect(thread).tasks.at(-1).controller_state).toBe('WORKING');
        await signed({ type: 'event_callback', team_id: team, event_id: `Ev_${randomUUID()}`, event: { type: 'agent_session_stopped', user, channel, thread_ts: thread } });
        await expect.poll(() => inspect(thread).tasks.find((t: any) => t.task_id === taskId)?.controller_state,
            { intervals: [1000, 2000, 5000] }).toBe('CANCELED');
        for (const completed of completedTasks) expect(inspect(thread).tasks.find((t: any) => t.task_id === completed.task_id)?.controller_state).toBe('COMPLETED');
    });
    await test.step('Web continues the Slack-bound A2A thread', async () => {
        const taskCount = inspect(thread).tasks.length;
        const threadId = inspect(thread).binding.thread_id as string;
        await page.goto(`${base}/?thread=${threadId}`);
        await page.locator('input[type=password]').fill(env.WEB_ACCESS_TOKEN!);
        await page.getByRole('button', { name: 'Continue', exact: true }).click();
        await expect(page.getByPlaceholder('Message Claude…')).toBeVisible();
        await page.getByPlaceholder('Message Claude…').fill(`Reply exactly WEB_${run}.`);
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect(page.locator('.assistant-message').last()).toContainText(`WEB_${run}`, { timeout: 120000 });
        await answer(`WEB_${run}`);
        await expect.poll(() => inspect(thread).tasks.length).toBe(taskCount + 1);
        const state = inspect(thread);
        expect(state.binding.thread_id).toBe(threadId);
        expect(state.legacy_tables_present).toEqual([]);
        expect(state.tasks.at(-1).client_message_id).toMatch(/^web:agui:/);
        expect(await responseCount(`WEB_${run}`)).toBe(1);
    });
    async function webSend(text: string): Promise<{ task_id: string; client_message_id: string }> {
        const previousCount = inspect(thread).tasks.length;
        await page.getByPlaceholder('Message Claude…').fill(text);
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect.poll(() => inspect(thread).tasks.length).toBe(previousCount + 1);
        const task = inspect(thread).tasks.at(-1);
        expect(task.client_message_id).toMatch(/^web:agui:/);
        return task;
    }
    await test.step('Web reload during WORKING reconnects to one A2A Task', async () => {
        const marker = `RELOADED_${run}`;
        const task = await webSend(`Write a detailed numbered list of twenty practical uses for event-driven systems. End with exactly ${marker}.`);
        expect(['SUBMITTED', 'WORKING']).toContain(inspect(thread).tasks.find((item: any) => item.task_id === task.task_id)?.controller_state);
        await page.reload();
        await expect(page.locator('.assistant-message').last()).toContainText(marker, { timeout: 120000 });
        await answer(marker);
        expect(inspect(thread).tasks.filter((item: any) => item.task_id === task.task_id)).toHaveLength(1);
        expect(await responseCount(marker)).toBe(1);
    });
    await test.step('Web automatic policy survives reload without controls', async () => {
        const task = await webSend(`Use web_fetch exactly once to fetch https://example.com and reply WEB_TOOL_${run}.`);
        await page.reload();
        await expect.poll(() => inspect(thread).tasks.find((t: any) => t.task_id === task.task_id)?.controller_state).toBe('COMPLETED');
        await expect(page.locator('.assistant-message').last()).toContainText(`WEB_TOOL_${run}`);
        await expect(page.getByRole('button', {name: 'Allow', exact: true})).toHaveCount(0);
        const requests = inspect(thread).tasks.find((t: any) => t.task_id === task.task_id).input_requests;
        expect(requests).toHaveLength(1);
        expect(requests[0]).toMatchObject({status: 'resolved', decision: 'allow'});
        await signed({type: 'block_actions', team: {id: team}, user: {id: user}, channel: {id: channel},
            message: {ts: root.ts, thread_ts: thread}, actions: [{action_id: 'agent_tool_deny',
                value: JSON.stringify({taskId: task.task_id, requestId: requests[0].request_id})}]}, true);
        expect(inspect(thread).tasks.find((t: any) => t.task_id === task.task_id).input_requests[0].decision).toBe('allow');
    });
    await test.step('Web Stop cancels a WORKING A2A Task', async () => {
        const task = await webSend('Write a detailed 10000-word tutorial on distributed systems, covering each component in depth.');
        await expect.poll(() => inspect(thread).tasks.find((item: any) => item.task_id === task.task_id)?.controller_state)
            .toBe('WORKING');
        await page.getByRole('button', { name: 'Stop', exact: true }).click();
        await expect.poll(() => inspect(thread).tasks.find((item: any) => item.task_id === task.task_id)?.controller_state)
            .toBe('CANCELED');
        await expect(page.locator('.tool-call')).toHaveCount(0);
        await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
        await page.reload();
        await expect(page.locator('.tool-call')).toHaveCount(0);
        expect(inspect(thread).tasks.filter((item: any) => item.task_id === task.task_id)).toHaveLength(1);
    });
    await test.step('Slack Stop clears a Web WORKING Task', async () => {
        const task = await webSend('Write a detailed 10000-word tutorial on distributed systems, covering each component in depth.');
        await expect.poll(() => inspect(thread).tasks.find((item: any) => item.task_id === task.task_id)?.controller_state)
            .toBe('WORKING');
        await signed({ type: 'event_callback', team_id: team, event_id: `Ev_${randomUUID()}`, event: { type: 'agent_session_stopped', user, channel, thread_ts: thread } });
        await expect.poll(() => inspect(thread).tasks.find((item: any) => item.task_id === task.task_id)?.controller_state,
            { intervals: [1000, 2000, 5000] }).toBe('CANCELED');
        await page.reload();
        await expect(page.locator('.tool-call')).toHaveCount(0);
        await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    });
});
