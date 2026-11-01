import { createHmac, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { createLiveSlackClient } from '../helpers/live-slack';

// Requires the native human-review.toml fixture on the sandbox scheduler.
// Prompts use genuine Slack delivery. Slack decisions are signed API fixtures,
// not native client clicks. Web approval/reload uses the deployed browser UI.
for (const outcome of ['allow', 'deny', 'stop'] as const) {
    test(`live native human review: ${outcome}`, async ({ page, request }) => {
        const env = process.env;
        expect(env.E2E_LIVE_HUMAN_REVIEW_POLICY, 'Deploy the isolated review policy before this suite').toBe('1');
        const base = env.E2E_LIVE_BASE_URL!;
        const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
        const team = env.E2E_LIVE_SLACK_TEAM_ID!;
        const slack = createLiveSlackClient(request, env);
        const bot = await slack('auth.test', {});
        expect((await request.post(`${base}/api/auth/session`, {
            data: { accessToken: env.WEB_ACCESS_TOKEN },
        })).ok()).toBe(true);
        const marker = `HUMAN_REVIEW_${randomUUID()}`;
        const root = await slack('chat.postMessage', {
            channel,
            text: `<@${bot.user_id}> Use web_search exactly once to search for example.com. After the tool succeeds or is denied, briefly describe the outcome and include ${marker}. If denied, do not retry or use another tool. Do not ask a clarification question.`,
        }, 'user');
        const progress: Record<string, unknown> = { outcome, channel, thread_ts: root.ts };
        console.log(JSON.stringify(progress));
        function inspect(provider = false): any {
            return JSON.parse(execFileSync('uv', [
                'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
                '--team-id', team, '--channel-id', channel, '--thread-ts', root.ts,
                ...(provider ? ['--provider-evidence'] : []),
            ], { encoding: 'utf8', timeout: 30000, env }));
        }
        const intervals = [1000, 2000, 5000];
        const replies = async (): Promise<any[]> => (await slack('conversations.replies', { channel, ts: root.ts, limit: 100 })).messages;
        let threadId: string | undefined;
        let terminal = false;
        try {
            await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('INPUT_REQUIRED');
            const pending = inspect(true);
            threadId = pending.binding.thread_id;
            const task = pending.tasks[0];
            progress.thread_id = threadId;
            progress.task_id = task.task_id;
            console.log(JSON.stringify(progress));
            expect(task.input_requests).toHaveLength(1);
            const review = task.input_requests[0];
            expect(review).toMatchObject({ status: 'pending', human_ruling: null, decision: null, confirmation_attempted_at: null });
            expect(review.review_digest).toMatch(/^[a-f0-9]{64}$/);
            expect(pending.provider_evidence).toEqual([expect.objectContaining({
                tool_name: 'web_search', confirmation_decisions: [], result_errors: [],
            })]);
            await expect.poll(async () => JSON.stringify(await replies()), { intervals }).toContain('agent_tool_allow');
            const controls = (await replies()).find(m => (JSON.stringify(m.blocks) || '').includes('agent_tool_allow'));
            expect(JSON.stringify(controls.blocks)).toContain('user-approval');
            const history = await request.get(`${base}/api/agui/threads/${threadId}/history`);
            const canonical = await history.json();
            expect(canonical.hasPendingInterrupts).toBe(true);
            expect(canonical.pendingRequestIds).toEqual([review.request_id]);
            expect(JSON.stringify(canonical.messages)).toContain('user-approval');
            const interrupts = canonical.messages.flatMap((m: any) => m.metadata?.custom?.agui?.interrupts || []);
            expect(interrupts).toHaveLength(1);
            expect(interrupts[0].metadata.tool.name).toBe('web_search');
            const slackCall = controls.blocks.find((block: any) => block.text?.text?.includes('*Agent wants to run a tool*'));
            expect(JSON.parse(slackCall.text.text.split('```')[1])).toEqual(interrupts[0].metadata.tool.arguments);
            await page.goto(`${base}/?thread=${threadId}`);
            await page.locator('input[type=password]').fill(env.WEB_ACCESS_TOKEN!);
            await page.getByRole('button', { name: 'Continue', exact: true }).click();
            await expect(page.locator('.interrupt-panel')).toContainText('user-approval');
            await page.reload();
            await expect(page.getByRole('button', { name: 'Allow', exact: true })).toBeVisible();
            async function signedDecision(decision: 'allow' | 'deny') {
                const raw = new URLSearchParams({ payload: JSON.stringify({
                    type: 'block_actions', team: { id: team }, user: { id: env.E2E_LIVE_SLACK_USER_ID },
                    channel: { id: channel }, message: { ts: controls.ts, thread_ts: root.ts },
                    actions: [{ action_id: `agent_tool_${decision}`, value: JSON.stringify({ taskId: task.task_id, requestId: review.request_id }), action_ts: String(Date.now()) }],
                }) }).toString();
                const timestamp = Math.floor(Date.now() / 1000).toString();
                const signature = createHmac('sha256', env.SLACK_SIGNING_SECRET!).update(`v0:${timestamp}:${raw}`).digest('hex');
                expect((await request.post(`${base}/slack/interactions`, { data: raw, headers: {
                    'content-type': 'application/x-www-form-urlencoded', 'x-slack-request-timestamp': timestamp,
                    'x-slack-signature': `v0=${signature}`,
                } })).ok()).toBe(true);
            }
            if (outcome === 'allow') {
                await page.getByRole('button', { name: 'Allow', exact: true }).click();
                await page.getByRole('button', { name: 'Submit responses' }).click();
            } else if (outcome === 'deny') {
                await signedDecision('deny');
                await signedDecision('deny');
            } else {
                expect((await request.post(`${base}/api/agui/threads/${threadId}/cancel`)).ok()).toBe(true);
            }
            await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe(outcome === 'stop' ? 'CANCELED' : 'COMPLETED');
            terminal = true;
            await expect(page.locator('.interrupt-panel')).toHaveCount(0);
            await expect.poll(async () => JSON.stringify(await replies()), { intervals }).not.toContain('agent_tool_allow');
            const after = inspect(true);
            if (outcome === 'stop') {
                expect(after.provider_evidence[0].confirmation_decisions).not.toContain('allow');
                expect(after.provider_evidence[0].result_errors).not.toContain(false);
                expect(after.interruption_evidence.interrupt_count).toBe(1);
            } else {
                expect(after.tasks[0].input_requests[0]).toMatchObject({ human_ruling: outcome, decision: outcome, status: 'resolved' });
                expect(after.provider_evidence[0]).toMatchObject({ confirmation_decisions: [outcome], result_errors: [outcome === 'deny'] });
                await expect.poll(async () => (await replies()).filter(m => m.user === bot.user_id && JSON.stringify(m.blocks?.length ? m.blocks : m.text).includes(marker)).length, { intervals }).toBe(1);
            }
            // Late conflicting controls cannot change the durable decision or repeat CMA confirmation.
            await signedDecision(outcome === 'allow' ? 'deny' : 'allow');
            await page.reload();
            await expect(page.locator('.interrupt-panel')).toHaveCount(0);
            const final = await request.get(`${base}/api/agui/threads/${threadId}/history`);
            expect((await final.json()).hasPendingInterrupts).toBe(false);
            expect(inspect(true).provider_evidence).toEqual(after.provider_evidence);
            console.log(JSON.stringify({ ...progress, state: after.tasks[0].controller_state, provider_evidence: after.provider_evidence, interruption_evidence: after.interruption_evidence }));
        } finally {
            if (!terminal) {
                threadId ||= inspect().binding?.thread_id;
                if (threadId) await request.post(`${base}/api/agui/threads/${threadId}/cancel`);
            }
        }
    });
}
