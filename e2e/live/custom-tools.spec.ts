import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { createLiveSlackClient } from '../helpers/live-slack';

test('live text_metrics executes once per Slack and Web invocation with native result admission', async ({ page, request }) => {
    const env = process.env;
    expect(env.RUN_LIVE_E2E).toBe('1');
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const team = env.E2E_LIVE_SLACK_TEAM_ID!;
    const slack = createLiveSlackClient(request, env);
    const bot = await slack('auth.test', {});
    expect((await request.post(`${base}/api/auth/session`, { data: { accessToken: env.WEB_ACCESS_TOKEN } })).ok()).toBe(true);
    const marker = `CUSTOM_TOOL_${randomUUID()}`;
    const prompt = (suffix: string) => `Call text_metrics exactly once with text equal to Aé🙂. Report its character and UTF-8 byte counts, then include ${marker}_${suffix}. Do not use another tool or count it yourself.`;
    const root = await slack('chat.postMessage', { channel, text: `<@${bot.user_id}> ${prompt('SLACK')}` }, 'user');
    function inspect(provider = false): any {
        return JSON.parse(execFileSync('uv', [
            'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
            '--team-id', team, '--channel-id', channel, '--thread-ts', root.ts,
            ...(provider ? ['--provider-evidence'] : []),
        ], { encoding: 'utf8', timeout: 30000, env }));
    }
    const intervals = [1000, 2000, 5000];
    let threadId: string | undefined;
    let terminal = false;
    try {
        await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('COMPLETED');
        const first = inspect(true);
        threadId = first.binding.thread_id;
        expect(first.tasks).toHaveLength(1);
        expect(first.provider_evidence).toEqual([expect.objectContaining({
            tool_name: 'text_metrics', confirmation_decisions: [],
            result_event_types: ['user.custom_tool_result'], result_errors: [false], metrics_match: true,
        })]);
        await page.goto(`${base}/?thread=${threadId}`);
        await page.locator('input[type=password]').fill(env.WEB_ACCESS_TOKEN!);
        await page.getByRole('button', { name: 'Continue', exact: true }).click();
        await expect(page.locator('.assistant-message').last()).toContainText(`${marker}_SLACK`);
        await page.getByPlaceholder('Message Claude…').fill(prompt('WEB'));
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect.poll(() => inspect().tasks.length, { intervals }).toBe(2);
        await expect.poll(() => inspect().tasks[1]?.controller_state, { intervals }).toBe('COMPLETED');
        terminal = true;
        await page.reload();
        await expect(page.locator('.assistant-message').last()).toContainText(`${marker}_WEB`);
        const after = inspect(true);
        expect(after.provider_evidence).toHaveLength(2);
        for (const evidence of after.provider_evidence) expect(evidence).toMatchObject({
            tool_name: 'text_metrics', confirmation_decisions: [],
            result_event_types: ['user.custom_tool_result'], result_errors: [false], metrics_match: true,
        });
        for (const task of after.tasks) {
            expect(task.tool_executions).toHaveLength(1);
            expect(task.tool_executions[0]).toMatchObject({ state: 'resolved', control_code: null });
            expect(task.tool_executions[0].execution_attempted_at).toBeTruthy();
            expect(task.tool_executions[0].delivery_attempted_at).toBeTruthy();
            expect(task.tool_executions[0].result_digest).toMatch(/^[a-f0-9]{64}$/);
            expect(task.input_requests).toEqual([expect.objectContaining({ decision: 'allow', status: 'resolved', confirmation_attempted_at: null })]);
        }
        await expect.poll(async () => {
            const replies = (await slack('conversations.replies', { channel, ts: root.ts, limit: 100 })).messages;
            return ['SLACK', 'WEB'].every(suffix => replies.filter((m: any) => m.user === bot.user_id && JSON.stringify(m.blocks?.length ? m.blocks : m.text).includes(`${marker}_${suffix}`)).length === 1);
        }, { intervals }).toBe(true);
        console.log(JSON.stringify({ thread_id: threadId, task_ids: after.tasks.map((t: any) => t.task_id), custom_results: 2, metrics_match: true }));
    } finally {
        if (!terminal) {
            threadId ||= inspect().binding?.thread_id;
            if (threadId) await request.post(`${base}/api/agui/threads/${threadId}/cancel`);
        }
    }
});
