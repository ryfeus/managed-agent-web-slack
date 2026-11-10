import { createHmac, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { createLiveSlackClient } from '../helpers/live-slack';

test('live synthetic redaction, human review and repeated-effect allow/deny', async ({ page, request }) => {
    const env = process.env;
    expect(env.RUN_LIVE_E2E).toBe('1');
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const slack = createLiveSlackClient(request, env);
    const bot = await slack('auth.test', {});
    expect((await request.post(`${base}/api/auth/session`, { data: { accessToken: env.WEB_ACCESS_TOKEN } })).ok()).toBe(true);
    const marker = `POLICY_DEMO_${randomUUID()}`;
    const root = await slack('chat.postMessage', { channel, text: `<@${bot.user_id}> Use demo_read_contact_report exactly once, then demo_prepare_release, then demo_publish_report with audience public and the exact admitted report text. Wait for each result. Use no other tools. Include ${marker} in your final response.` }, 'user');
    const inspect = (provider = false): any => JSON.parse(execFileSync('uv', [
        'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
        '--team-id', env.E2E_LIVE_SLACK_TEAM_ID!, '--channel-id', channel, '--thread-ts', root.ts,
        ...(provider ? ['--provider-evidence'] : []),
    ], { encoding: 'utf8', timeout: 30000, env }));
    const intervals = [1000, 2000, 5000];
    let threadId: string | undefined;
    let terminal = false;
    try {
        await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('INPUT_REQUIRED');
        threadId = inspect().binding.thread_id;
        await page.goto(`${base}/?thread=${threadId}`);
        await page.locator('input[type=password]').fill(env.WEB_ACCESS_TOKEN!);
        await page.getByRole('button', { name: 'Continue', exact: true }).click();
        await expect(page.locator('.interrupt-panel')).toContainText('demo_publish_report');
        await page.reload();
        await expect(page.locator('.interrupt-panel')).toContainText('demo_publish_report');
        await expect(page.locator('.interrupt-panel')).toContainText('[redacted-email]');
        expect(await page.locator('body').innerText()).not.toContain('alex@example.com');
        await page.getByRole('button', { name: 'Allow', exact: true }).click();
        await page.getByRole('button', { name: 'Submit responses' }).click();
        await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('COMPLETED');
        const first = inspect(true);
        expect(first.provider_evidence).toEqual(expect.arrayContaining([
            expect.objectContaining({ tool_name: 'demo_read_contact_report', redaction_match: true, result_errors: [false] }),
            expect.objectContaining({ tool_name: 'demo_publish_report', published_admitted_report: true, result_errors: [false] }),
        ]));
        await page.getByPlaceholder('Message Claude…').fill('Repeat demo_publish_report exactly once with the same audience public and the exact same admitted report text. Use no other tools.');
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect(page.locator('.interrupt-panel')).toContainText('demo.report_published');
        await page.getByRole('button', { name: 'Deny', exact: true }).click();
        await page.getByRole('button', { name: 'Submit responses' }).click();
        await expect.poll(() => inspect().tasks[1]?.controller_state, { intervals }).toBe('COMPLETED');
        const after = inspect(true);
        const repeated = after.tasks[1].tool_executions;
        expect(repeated).toHaveLength(1);
        expect(repeated[0]).toMatchObject({ control_code: 'denied', execution_attempted_at: null, state: 'resolved' });
        // The native human denial persists for the exact call; review a changed proposal.
        await page.getByPlaceholder('Message Claude…').fill("Call demo_publish_report exactly once with audience public and text 'Synthetic follow-up announcement'. Use no other tools.");
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect(page.locator('.interrupt-panel')).toContainText('demo.report_published');
        await page.getByRole('button', { name: 'Allow', exact: true }).click();
        await page.getByRole('button', { name: 'Submit responses' }).click();
        await expect.poll(() => inspect().tasks[2]?.controller_state, { intervals }).toBe('COMPLETED');
        const waived = inspect(true);
        expect(waived.tasks[2].tool_executions).toHaveLength(1);
        expect(waived.tasks[2].tool_executions[0].execution_attempted_at).toBeTruthy();
        expect(waived.tasks[2].input_requests[0]).toMatchObject({ human_ruling: 'allow', decision: 'allow', status: 'resolved' });
        const replies = await slack('conversations.replies', { channel, ts: root.ts, limit: 100 });
        expect(JSON.stringify(replies.messages)).not.toContain('alex@example.com');
        terminal = true;
        console.log(JSON.stringify({ thread_id: threadId, task_ids: waived.tasks.map((t: any) => t.task_id), redaction_match: true, denied_repeat_executed: false, approved_repeat_executed: true }));
    } finally {
        if (!terminal) {
            threadId ||= inspect().binding?.thread_id;
            if (threadId) await request.post(`${base}/api/agui/threads/${threadId}/cancel`);
        }
    }
});


test('live private state blocks public release and signed Slack can approve an internal release', async ({ page, request }) => {
    const env = process.env;
    expect(env.RUN_LIVE_E2E).toBe('1');
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const slack = createLiveSlackClient(request, env);
    const bot = await slack('auth.test', {});
    expect((await request.post(`${base}/api/auth/session`, { data: { accessToken: env.WEB_ACCESS_TOKEN } })).ok()).toBe(true);
    const root = await slack('chat.postMessage', { channel, text: `<@${bot.user_id}> Use demo_read_private_report exactly once. Report the synthetic result, then stop. Do not call any other tool. PRIVATE_STATE_${randomUUID()}` }, 'user');
    const inspect = (provider = false): any => JSON.parse(execFileSync('uv', [
        'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
        '--team-id', env.E2E_LIVE_SLACK_TEAM_ID!, '--channel-id', channel, '--thread-ts', root.ts,
        ...(provider ? ['--provider-evidence'] : []),
    ], { encoding: 'utf8', timeout: 30000, env }));
    const intervals = [1000, 2000, 5000];
    let threadId: string | undefined;
    let terminal = false;
    try {
        await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('INPUT_REQUIRED');
        threadId = inspect().binding.thread_id;
        await page.goto(`${base}/?thread=${threadId}`);
        await page.locator('input[type=password]').fill(env.WEB_ACCESS_TOKEN!);
        await page.getByRole('button', { name: 'Continue', exact: true }).click();
        await expect(page.locator('.interrupt-panel')).toContainText('demo_read_private_report');
        await page.reload();
        await expect(page.locator('.interrupt-panel')).toContainText('demo_read_private_report');
        await page.getByRole('button', { name: 'Allow', exact: true }).click();
        await page.getByRole('button', { name: 'Submit responses' }).click();
        await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('COMPLETED');
        await page.getByPlaceholder('Message Claude…').fill("Use demo_prepare_release, then attempt demo_publish_report with audience public and text 'Synthetic summary'. After denial stop; do not use any other tool or change audience.");
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect.poll(() => inspect().tasks[1]?.controller_state, { intervals }).toBe('COMPLETED');
        let current = inspect(true);
        const blocked = current.provider_evidence.find((e: any) => e.tool_name === 'demo_publish_report');
        expect(blocked).toMatchObject({ confirmation_decisions: [], result_errors: [true] });
        expect(current.tasks[1].input_requests.find((r: any) => r.request_id === blocked.request_id)).toMatchObject({ decision: 'deny', human_ruling: null });
        expect(current.tasks[1].tool_executions.filter((e: any) => e.control_code === 'denied')).toEqual([expect.objectContaining({ execution_attempted_at: null })]);
        await page.getByPlaceholder('Message Claude…').fill("Use demo_publish_report exactly once with audience internal and text 'Synthetic summary'. Use no other tools.");
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect.poll(() => inspect().tasks[2]?.controller_state, { intervals }).toBe('INPUT_REQUIRED');
        current = inspect();
        const task = current.tasks[2];
        const review = task.input_requests[0];
        const replies = async (): Promise<any[]> => (await slack('conversations.replies', { channel, ts: root.ts, limit: 100 })).messages;
        await expect.poll(async () => JSON.stringify(await replies()), { intervals }).toContain(review.request_id);
        const card = (await replies()).find(m => JSON.stringify(m.blocks)?.includes(review.request_id));
        const raw = new URLSearchParams({ payload: JSON.stringify({ type: 'block_actions',
            team: { id: env.E2E_LIVE_SLACK_TEAM_ID }, user: { id: env.E2E_LIVE_SLACK_USER_ID },
            channel: { id: channel }, message: { ts: card.ts, thread_ts: root.ts },
            actions: [{ action_id: 'agent_tool_allow', value: JSON.stringify({ taskId: task.task_id, requestId: review.request_id }), action_ts: String(Date.now()) }],
        }) }).toString();
        const timestamp = Math.floor(Date.now() / 1000).toString();
        const signature = createHmac('sha256', env.SLACK_SIGNING_SECRET!).update(`v0:${timestamp}:${raw}`).digest('hex');
        for (let i = 0; i < 2; i++) expect((await request.post(`${base}/slack/interactions`, { data: raw, headers: {
            'content-type': 'application/x-www-form-urlencoded', 'x-slack-request-timestamp': timestamp, 'x-slack-signature': `v0=${signature}`,
        } })).ok()).toBe(true);
        await expect.poll(() => inspect().tasks[2]?.controller_state, { intervals }).toBe('COMPLETED');
        await page.reload();
        await expect(page.locator('.interrupt-panel')).toHaveCount(0);
        const after = inspect(true);
        expect(after.tasks[2].tool_executions).toHaveLength(1);
        expect(after.tasks[2].tool_executions[0]).toMatchObject({ state: 'resolved', control_code: null });
        expect(after.provider_evidence.filter((e: any) => e.request_id === review.request_id)).toEqual([expect.objectContaining({ result_errors: [false], confirmation_decisions: [] })]);
        terminal = true;
        console.log(JSON.stringify({ thread_id: threadId, task_ids: after.tasks.map((t: any) => t.task_id), public_denied: true, internal_review_allowed: true, signed_slack_duplicate: true }));
    } finally {
        if (!terminal) {
            threadId ||= inspect().binding?.thread_id;
            if (threadId) await request.post(`${base}/api/agui/threads/${threadId}/cancel`);
        }
    }
});
