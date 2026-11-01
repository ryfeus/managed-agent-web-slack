import { writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';
import { createLiveSlackClient } from '../helpers/live-slack';

// Genuine Slack user-token delivery. OpenAPPA owns the decision; no interaction fixture.
// Run deny with a separately deployed policy omitting web_search and a fresh context.
test('live Slack tool is automatically decided and renders one final answer', async ({ request }) => {
    const env = process.env;
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const team = env.E2E_LIVE_SLACK_TEAM_ID!;
    const user = env.E2E_LIVE_SLACK_USER_ID!;
    const tool = env.E2E_LIVE_POLICY_TOOL || 'web_fetch';
    const decision = env.E2E_LIVE_POLICY_DECISION || 'allow';
    expect(['web_fetch', 'web_search']).toContain(tool);
    expect(['allow', 'deny']).toContain(decision);
    const intervals = [1000, 2000, 5000];
    const slack = createLiveSlackClient(request, env);
    const bot = await slack('auth.test', {});
    const human = await slack('auth.test', {}, 'user');
    expect(bot.team_id).toBe(team);
    expect(human.team_id).toBe(team);
    expect(human.user_id).toBe(user);
    const login = await request.post(`${base}/api/auth/session`, { data: { accessToken: env.WEB_ACCESS_TOKEN } });
    expect(login.ok()).toBe(true);
    const marker = `POLICY_VISIBLE_${randomUUID()}`;
    const action = tool === 'web_fetch' ? 'fetch https://example.com' : 'search for example.com';
    const root = await slack('chat.postMessage', {
        channel,
        text: `<@${bot.user_id}> Use ${tool} exactly once to ${action}. After the tool succeeds or is denied, reply with a brief description of that outcome followed by ${marker}. If denied, do not retry or use another tool. Do not ask me for approval.`,
    }, 'user');
    const progress: Record<string, unknown> = { run_id: env.E2E_RUN_ID, channel, thread_ts: root.ts };
    const recordProgress = () => {
        console.log(JSON.stringify(progress));
        if (env.E2E_RUN_METADATA) writeFileSync(env.E2E_RUN_METADATA, JSON.stringify(progress));
    };
    recordProgress();
    function inspect(provider = false): any {
        return JSON.parse(execFileSync('uv', [
            'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
            '--team-id', team, '--channel-id', channel, '--thread-ts', root.ts,
            ...(provider ? ['--provider-evidence'] : []),
        ], { encoding: 'utf8', timeout: 30000, env: { ...env, PYTHONDONTWRITEBYTECODE: '1' } }));
    }
    async function replies(): Promise<any[]> {
        return (await slack('conversations.replies', { channel, ts: root.ts, limit: 100 })).messages;
    }
    function visibleText(value: any): string {
        if (Array.isArray(value)) return value.map(visibleText).join('\n');
        if (!value || typeof value !== 'object') return '';
        return Object.entries(value).map(([key, item]) =>
            (key === 'text' || key === 'markdown_text') && typeof item === 'string'
                ? item : visibleText(item)).join('\n');
    }
    const answerText = (m: any): string => m.blocks?.length ? visibleText(m.blocks) : m.text || '';
    await expect.poll(() => inspect().tasks[0]?.controller_state, { intervals }).toBe('COMPLETED');
    const after = inspect(true);
    expect(after.provider_evidence).toHaveLength(1);
    expect(after.provider_evidence[0]).toMatchObject({tool_name:tool,confirmation_decisions:[decision]});
    expect(after.provider_evidence[0].result_event_types).toContain('agent.tool_result');
    expect(after.provider_evidence[0].result_errors).toEqual([decision === 'deny']);
    expect(after.tasks).toHaveLength(1);
    const task = after.tasks[0];
    progress.task_id = task.task_id;
    progress.thread_id = after.binding.thread_id;
    recordProgress();
    expect(task.input_requests).toHaveLength(1);
    expect(task.input_requests[0]).toMatchObject({status: 'resolved', decision});
    expect(task.input_requests[0].confirmation_attempted_at).toBeTruthy();
    await expect.poll(() => inspect().tasks[0]?.projection_status, { intervals }).toBe('completed');
    const visibleAnswers = async () => (await replies()).filter(m =>
        m.ts !== root.ts && m.user === bot.user_id && answerText(m).includes(marker));
    await expect.poll(async () => (await visibleAnswers()).length, { intervals }).toBe(1);
    const [answer] = await visibleAnswers();
    expect(answerText(answer).split(marker)).toHaveLength(2);
    expect(JSON.stringify(await replies())).not.toContain('agent_tool_allow');
    const history = await request.get(`${base}/api/agui/threads/${after.binding.thread_id}/history`);
    expect(history.ok()).toBe(true);
    const canonical = await history.json();
    expect(canonical.messages.flatMap((m: any) => m.toolCalls || [])).toHaveLength(0);
    expect(canonical.messages.filter((m: any) => m.role === 'assistant' && m.content?.includes(marker))).toHaveLength(1);
    console.log(JSON.stringify({thread_id: after.binding.thread_id, task_id: task.task_id,
        response_ts: answer.ts, policy_decision: decision, visible_answer_count: 1,
        confirmation_attempted_at: task.input_requests[0].confirmation_attempted_at, provider_evidence: after.provider_evidence}));
});
