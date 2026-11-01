import { writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { createLiveSlackClient } from '../helpers/live-slack';

// Genuine Slack ingress; clarification is answered through the linked web API.
// This does not simulate or claim native Slack button/modal interaction coverage.
for (const stop of [false, true]) {
    test(`live Slack clarification ${stop ? 'supports explicit Stop' : 'resumes with a human answer'}`, async ({ request }) => {
        const env = process.env;
        const base = env.E2E_LIVE_BASE_URL!;
        const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
        const team = env.E2E_LIVE_SLACK_TEAM_ID!;
        const slack = createLiveSlackClient(request, env);
        const bot = await slack('auth.test', {});
        const human = await slack('auth.test', {}, 'user');
        expect(bot.team_id).toBe(team);
        expect(human.team_id).toBe(team);
        expect(human.user_id).toBe(env.E2E_LIVE_SLACK_USER_ID);
        expect((await request.post(`${base}/api/auth/session`, {
            data: { accessToken: env.WEB_ACCESS_TOKEN },
        })).ok()).toBe(true);
        const marker = `CLARIFICATION_VISIBLE_${randomUUID()}`;
        const answer = `SANDBOX_${randomUUID()}`;
        const root = await slack('chat.postMessage', {
            channel,
            text: `<@${bot.user_id}> Use the custom tool ask_user exactly once with prompt "Which environment should I use?". Wait for the answer. Do not use any other tool. After receiving the answer, reply with the exact answer followed by ${marker}.`,
        }, 'user');
        const progress: Record<string, unknown> = {
            run_id: env.E2E_RUN_ID, channel, thread_ts: root.ts, clarification_action: stop ? 'stop' : 'answer',
        };
        const record = () => {
            console.log(JSON.stringify(progress));
            if (env.E2E_RUN_METADATA) writeFileSync(env.E2E_RUN_METADATA, JSON.stringify(progress));
        };
        record();
        function inspect(provider = false): any {
            return JSON.parse(execFileSync('uv', [
                'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
                '--team-id', team, '--channel-id', channel, '--thread-ts', root.ts,
                ...(provider ? ['--provider-evidence'] : []),
            ], { encoding: 'utf8', timeout: 30000, env: { ...env, PYTHONDONTWRITEBYTECODE: '1' } }));
        }
        const intervals = [1000, 2000, 5000];
        let threadId: string | undefined;
        let terminal = false;
        try {
            await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('INPUT_REQUIRED');
            const pending = inspect(true);
            threadId = pending.binding.thread_id;
            progress.thread_id = threadId;
            progress.task_id = pending.tasks[0].task_id;
            record();
            expect(pending.tasks).toHaveLength(1);
            expect(pending.tasks[0].input_requests).toHaveLength(0);
            expect(pending.tasks[0].clarification_requests).toHaveLength(1);
            expect(pending.tasks[0].clarification_requests[0].status).toBe('pending');
            expect(pending.clarification_evidence).toEqual([expect.objectContaining({
                tool_name: 'ask_user', source_event_type: 'agent.custom_tool_use', result_count: 0,
            })]);
            await expect.poll(() => inspect().tasks[0]?.projection_status, { intervals }).toBe('completed');
            const replies = async () => (await slack('conversations.replies', { channel, ts: root.ts, limit: 100 })).messages;
            await expect.poll(async () => JSON.stringify(await replies()), { intervals }).toContain('Which environment should I use?');
            expect(JSON.stringify(await replies())).not.toContain('agent_tool_allow');
            const historyResponse = await request.get(`${base}/api/agui/threads/${threadId}/history`);
            expect(historyResponse.ok()).toBe(true);
            const history = await historyResponse.json();
            expect(history.hasPendingInterrupts).toBe(true);
            const interrupts = history.messages.flatMap((m: any) => m.metadata?.custom?.agui?.interrupts || []);
            expect(interrupts).toHaveLength(1);
            expect(interrupts[0].reason).toBe('clarification');
            expect(interrupts[0].id).toBe(pending.tasks[0].clarification_requests[0].request_id);
            if (stop) {
                expect((await request.post(`${base}/api/agui/threads/${threadId}/cancel`)).ok()).toBe(true);
                await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('CANCELED');
            } else {
                const resumed = await request.post(`${base}/api/agui`, {
                    data: {
                        threadId, runId: randomUUID(), state: {}, messages: [], tools: [], context: [], forwardedProps: {},
                        resume: [{ interruptId: interrupts[0].id, status: 'resolved', payload: { answer } }],
                    },
                    timeout: 240000,
                });
                expect(resumed.ok()).toBe(true);
                expect(await resumed.text()).not.toContain('RUN_ERROR');
                await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('COMPLETED');
            }
            terminal = true;
            const after = inspect(true);
            expect(after.tasks).toHaveLength(1);
            if (stop) {
                // CMA can terminate the suspended call directly on interrupt, without requesting a custom result.
                expect(after.interruption_evidence).toEqual({ interrupt_count: 1, terminal_after_interrupt: true });
                expect(after.clarification_evidence[0].result_errors).not.toContain(false);
                const canceled = await request.get(`${base}/api/agui/threads/${threadId}/history`);
                expect((await canceled.json()).hasPendingInterrupts).toBe(false);
            } else {
                expect(after.tasks[0].clarification_requests[0].status).toBe('resolved');
                expect(after.clarification_evidence).toEqual([expect.objectContaining({
                    tool_name: 'ask_user', result_count: 1, result_errors: [false],
                })]);
            }
            await expect.poll(() => inspect().tasks[0]?.projection_status, { intervals }).toBe('completed');
            if (!stop) {
                // Inspect visible Slack blocks through the API, without copying transcript bodies into evidence logs.
                const answers = async () => (await replies()).filter((m: any) => m.ts !== root.ts && m.user === bot.user_id && JSON.stringify(m.blocks?.length ? m.blocks : m.text).includes(marker));
                await expect.poll(async () => (await answers()).length, { intervals }).toBe(1);
                expect(JSON.stringify((await answers())[0])).toContain(answer);
                const finalHistory = await request.get(`${base}/api/agui/threads/${threadId}/history`);
                const canonical = await finalHistory.json();
                expect(canonical.hasPendingInterrupts).toBe(false);
                expect(canonical.messages.filter((m: any) => m.role === 'assistant' && m.content?.includes(marker))).toHaveLength(1);
            }
            console.log(JSON.stringify({ ...progress, state: after.tasks[0].controller_state, interruption_evidence: after.interruption_evidence, clarification_evidence: after.clarification_evidence }));
        } finally {
            if (!terminal) {
                threadId ||= inspect().binding?.thread_id;
                if (threadId) await request.post(`${base}/api/agui/threads/${threadId}/cancel`);
            }
        }
    });
}
