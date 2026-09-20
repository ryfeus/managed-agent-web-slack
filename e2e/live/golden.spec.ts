import { createHmac, randomUUID } from 'node:crypto';
import { test, expect } from '@playwright/test';
// Opt-in only. A bot-created root anchors signed synthetic human events in a
// designated developer sandbox; all downstream DSQL/Anthropic/Slack work is real.
test('live sandbox: mention, continuation, approval, stop, shared session', async ({ page, request }) => {
    const env = process.env;
    const base = env.E2E_LIVE_BASE_URL!;
    const channel = env.E2E_LIVE_SLACK_CHANNEL_ID!;
    const team = env.E2E_LIVE_SLACK_TEAM_ID!;
    const user = env.E2E_LIVE_SLACK_USER_ID!;
    async function slack(method: string, data: Record<string, string | number | boolean>): Promise<any> {
        const response = await request.post(`https://slack.com/api/${method}`, {
            headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}` },
            form: data,
        });
        const result = await response.json();
        expect(result.ok, result.error).toBeTruthy();
        return result;
    }
    async function signed(payload: object, interaction = false) {
        const raw = interaction ? new URLSearchParams({ payload: JSON.stringify(payload) }).toString() : JSON.stringify(payload);
        const ts = Math.floor(Date.now() / 1000).toString();
        const sig = createHmac('sha256', env.SLACK_SIGNING_SECRET!).update(`v0:${ts}:${raw}`).digest('hex');
        const response = await request.post(`${base}/slack/events`, { data: raw, headers: { 'content-type': interaction ? 'application/x-www-form-urlencoded' : 'application/json', 'x-slack-request-timestamp': ts, 'x-slack-signature': `v0=${sig}` } });
        expect(response.ok()).toBeTruthy();
    }
    const auth = await slack('auth.test', {});
    const run = randomUUID();
    const root = await slack('chat.postMessage', { channel, text: `Local E2E live sandbox ${run}` });
    const thread = root.ts;
    async function input(text: string, first = false) {
        const receipt = first ? root : await slack('chat.postMessage', { channel, thread_ts: thread, text: `E2E input: ${text}` });
        await signed({ type: 'event_callback', team_id: team, event_id: `Ev_${randomUUID()}`, event: { type: first ? 'app_mention' : 'message', user, channel, ts: receipt.ts, thread_ts: thread, text: first ? `<@${auth.user_id}> ${text}` : text } });
    }
    async function replies(): Promise<any[]> { return (await slack('conversations.replies', { channel, ts: thread, limit: 100 })).messages; }
    async function answer(marker: string) {
        await expect.poll(async () => (await replies()).some(m => m.ts !== root.ts && !m.text?.startsWith('E2E input:') && JSON.stringify(m).includes(marker)), { intervals: [1000, 2000, 5000] }).toBe(true);
    }
    await test.step('Slack app mention', async () => {
        await input(`Reply exactly FIRST_${run}.`, true);
        await answer(`FIRST_${run}`);
    });
    await test.step('bound-thread continuation', async () => {
        await input(`Reply exactly SECOND_${run}.`);
        await answer(`SECOND_${run}`);
    });
    await test.step('tool approval', async () => {
        await input('Use web_fetch to fetch https://example.com and then briefly summarize it.');
        let message: any;
        await expect.poll(async () => {
            message = (await replies()).find(m => m.blocks?.some((b: any) => b.elements?.some((e: any) => e.action_id === 'agent_tool_allow')));
            return Boolean(message);
        }, { intervals: [1000, 2000, 5000] }).toBe(true);
        const action = message.blocks.flatMap((b: any) => b.elements || []).find((e: any) => e.action_id === 'agent_tool_allow');
        await signed({ type: 'block_actions', team: { id: team }, user: { id: user }, channel: { id: channel }, message: { ts: message.ts, thread_ts: thread }, actions: [{ ...action, action_ts: String(Date.now()) }] }, true);
        await expect.poll(async () => JSON.stringify((await replies()).find(m => m.ts === message.ts)?.blocks || []).includes('agent_tool_allow'), { intervals: [1000, 2000, 5000] }).toBe(false);
    });
    await page.goto(base);
    await page.locator('input[type=password]').fill(env.WEB_ACCESS_TOKEN!);
    await page.getByRole('button', { name: 'Continue', exact: true }).click();
    await expect(page.getByPlaceholder('Message Claude…')).toBeVisible();
    const sessions = await page.evaluate(async () => (await (await fetch('/api/sessions')).json()).data);
    const session = sessions.find((s: any) => s.title?.includes(run));
    expect(session, 'The sandbox Slack identity must map to the web principal').toBeTruthy();
    await page.goto(`${base}/?session=${session.id}`);
    // The browser UI is covered by the local production bundle suite. Here,
    // wait for the canonical Slack turn through the authenticated browser API
    // before sending a browser-surface message to the same session. This avoids
    // a sidebar-order race in a shared, long-lived sandbox.
    await expect.poll(async () => await page.evaluate(async ({ id, marker }) => {
        const events = (await (await fetch(`/api/sessions/${id}/events`)).json()).data || [];
        return events.some((event: unknown) => JSON.stringify(event).includes(marker));
    }, { id: session.id, marker: `FIRST_${run}` }), { intervals: [1000, 2000] }).toBe(true);
    await expect.poll(async () => await page.evaluate(async (id: string) => (await (await fetch(`/api/sessions/${id}`)).json()).status, session.id), { intervals: [1000, 2000] }).toBe('idle');
    await test.step('stop', async () => {
        await input('Run a long task: count to 10000, explaining each number, until interrupted.');
        await expect.poll(async () => await page.evaluate(async (id: string) => (await (await fetch(`/api/sessions/${id}`)).json()).status, session.id), { intervals: [500, 1000] }).toBe('running');
        await signed({ type: 'event_callback', team_id: team, event_id: `Ev_${randomUUID()}`, event: { type: 'agent_session_stopped', user, channel, thread_ts: thread } });
        await expect.poll(async () => await page.evaluate(async (id: string) => (await (await fetch(`/api/sessions/${id}`)).json()).status, session.id), { intervals: [1000, 2000] }).not.toBe('running');
    });
    await test.step('browser and Slack share the session', async () => {
        const accepted = await page.evaluate(async ({ id, text }) => {
            const response = await fetch(`/api/sessions/${id}/messages`, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ clientRequestId: crypto.randomUUID(), text }),
            });
            return response.status;
        }, { id: session.id, text: `Reply exactly SHARED_${run}.` });
        expect(accepted).toBe(202);
        await expect.poll(async () => await page.evaluate(async (id: string) => (await (await fetch(`/api/sessions/${id}`)).json()).status, session.id), { intervals: [1000, 2000] }).toBe('idle');
        await answer(`SHARED_${run}`);
    });
});
