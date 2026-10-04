import { createHmac, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { test, expect } from '@playwright/test';

// A genuine user-token mention exercises Slack delivery. The bot token reads the
// approval; the signed interaction fixture exercises the same ingress as Allow.
// This checks Slack's returned visible blocks, not a Slack client's button UI.
test('live Slack tool approval renders the final answer in the thread', async ({ request }) => {
    const env = process.env;
    expect(env.SLACK_USER_TOKEN, 'SLACK_USER_TOKEN is required for genuine Slack delivery').toBeTruthy();
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const team = env.E2E_LIVE_SLACK_TEAM_ID!;
    const user = env.E2E_LIVE_SLACK_USER_ID!;
    const intervals = [1000, 2000, 5000];
    async function slack(method: string, data: Record<string, string | number>, token = env.SLACK_BOT_TOKEN!) {
        const response = await request.post(`https://slack.com/api/${method}`, {
            headers: { authorization: `Bearer ${token}` }, form: data,
        });
        const result = await response.json();
        expect(result.ok, result.error).toBe(true);
        return result;
    }
    const bot = await slack('auth.test', {});
    const human = await slack('auth.test', {}, env.SLACK_USER_TOKEN!);
    expect(bot.team_id).toBe(team);
    expect(human.team_id).toBe(team);
    expect(human.user_id).toBe(user);
    const login = await request.post(`${base}/api/auth/session`, { data: { accessToken: env.WEB_ACCESS_TOKEN } });
    expect(login.ok()).toBe(true);
    const marker = `APPROVAL_VISIBLE_${randomUUID()}`;
    const root = await slack('chat.postMessage', {
        channel,
        text: `<@${bot.user_id}> Use web_fetch exactly once to fetch https://example.com. After approval and successful fetch, reply with a brief summary followed by ${marker}. Do not use other tools.`,
    }, env.SLACK_USER_TOKEN!);
    console.log(JSON.stringify({ marker, channel, thread_ts: root.ts }));
    function inspect(): any {
        return JSON.parse(execFileSync('uv', [
            'run', '--directory', 'backend', 'python', '../scripts/live_a2a_inspect.py',
            '--team-id', team, '--channel-id', channel, '--thread-ts', root.ts,
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
    function answerText(message: any): string {
        return message.blocks?.length ? visibleText(message.blocks) : message.text || '';
    }
    let approval: any;
    await expect.poll(async () => {
        approval = (await replies()).find(m => m.user === bot.user_id && m.blocks?.some((b: any) =>
            b.elements?.some((e: any) => e.action_id === 'agent_tool_allow')));
        return Boolean(approval);
    }, { intervals }).toBe(true);
    const action = approval.blocks.flatMap((b: any) => b.elements || [])
        .find((e: any) => e.action_id === 'agent_tool_allow');
    const ids = JSON.parse(action.value);
    const before = inspect();
    expect(before.tasks).toHaveLength(1);
    expect(before.tasks[0].task_id).toBe(ids.taskId);
    expect(before.tasks[0].controller_state).toBe('INPUT_REQUIRED');
    const threadId = before.binding.thread_id;
    async function history(): Promise<any> {
        const response = await request.get(`${base}/api/agui/threads/${threadId}/history`);
        expect(response.ok()).toBe(true);
        return response.json();
    }
    // Independently inspect the complete controller-derived input before allowing it.
    const pending = await history();
    expect(pending.activeTaskId).toBe(ids.taskId);
    const tools = pending.messages.flatMap((m: any) => m.toolCalls || []);
    expect(tools).toHaveLength(1);
    expect(tools[0].id).toBe(ids.requestId);
    expect(tools[0].function.name).toBe('web_fetch');
    expect(JSON.parse(tools[0].function.arguments)).toEqual({ url: 'https://example.com' });
    const payload = {
        type: 'block_actions', team: { id: team }, user: { id: user }, channel: { id: channel },
        message: { ts: approval.ts, thread_ts: root.ts },
        actions: [{ action_id: 'agent_tool_allow', value: action.value }],
    };
    const raw = new URLSearchParams({ payload: JSON.stringify(payload) }).toString();
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = createHmac('sha256', env.SLACK_SIGNING_SECRET!)
        .update(`v0:${timestamp}:${raw}`).digest('hex');
    const allowed = await request.post(`${base}/slack/interactions`, { data: raw, headers: {
        'content-type': 'application/x-www-form-urlencoded',
        'x-slack-request-timestamp': timestamp, 'x-slack-signature': `v0=${signature}`,
    } });
    expect(allowed.ok()).toBe(true);
    await expect.poll(() => inspect().tasks[0].controller_state, { intervals }).toBe('COMPLETED');
    const visibleAnswers = async () => (await replies()).filter(m =>
        m.ts !== root.ts && m.user === bot.user_id && answerText(m).includes(marker));
    await expect.poll(async () => (await visibleAnswers()).length, { intervals }).toBe(1);
    const [answer] = await visibleAnswers();
    expect(answerText(answer).split(marker)).toHaveLength(2);
    expect(answer.blocks.some((b: any) => b.type === 'context_actions')).toBe(true);
    expect((await replies()).find(m => m.ts === approval.ts)?.text).toContain('Allowed by');
    await expect.poll(async () => (await history()).messages.filter((m: any) =>
        m.role === 'assistant' && m.content?.includes(marker)).length, { intervals }).toBe(1);
    const after = inspect();
    expect(after.tasks).toHaveLength(1);
    expect(after.tasks[0].task_id).toBe(ids.taskId);
    expect(after.tasks[0].projection_status).toBe('completed');
    expect(after.tasks[0].input_requests).toEqual([
        expect.objectContaining({ request_id: ids.requestId, status: 'resolved', decision: 'allow' }),
    ]);
    console.log(JSON.stringify({ thread_id: threadId, task_id: ids.taskId, response_ts: answer.ts,
        block_types: answer.blocks.map((b: any) => b.type), visible_answer_count: 1 }));
});
