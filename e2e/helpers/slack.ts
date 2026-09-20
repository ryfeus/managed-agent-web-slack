import { createHmac, randomUUID } from 'node:crypto';
import { expect, type APIRequestContext } from '@playwright/test';
import { backend, control, state, ready, answerEvents } from './api';
export function mention(text: string, overrides: Record<string, unknown> = {}, eventId: string = randomUUID()) {
    return { type: 'event_callback', team_id: 'T001', event_id: eventId,
        event: { type: 'app_mention', user: 'U001', channel: 'C001', ts: '100.000001', text: `<@BOT> ${text}`, ...overrides } };
}
export async function inject(api: APIRequestContext, payload: object, interaction = false, valid = true) {
    const raw = interaction ? new URLSearchParams({ payload: JSON.stringify(payload) }).toString() : JSON.stringify(payload);
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = createHmac('sha256', 'e2e-slack-secret').update(`v0:${timestamp}:${raw}`).digest('hex');
    const response = await api.post(`${backend}/slack/events`, { data: raw, headers: {
            'content-type': interaction ? 'application/x-www-form-urlencoded' : 'application/json',
            'x-slack-request-timestamp': timestamp, 'x-slack-signature': valid ? `v0=${signature}` : 'v0=invalid'
        } });
    expect(response.status()).toBe(valid ? 200 : 401);
}
export async function slackTurn(api: APIRequestContext, prompt = 'hello', answer = 'Hello from Claude', overrides = {}) {
    const events = answerEvents(answer).map(e => ({ ...e, ...(e.id ? { id: `${prompt}_final` } : {}),
        ...(e.event ? { event: { ...e.event, id: `${prompt}_final` } } : {}), ...(e.event_id ? { event_id: `${prompt}_final` } : {}) }));
    await control(api, 'agent/script', { prompt, events, automatic: false });
    await inject(api, mention(prompt, overrides));
    const delivery = control(api, 'events/drain');
    await ready(api);
    const id = (await state(api)).agent.sessions[0].id as string;
    await control(api, 'agent/advance', { session_id: id });
    const result = await delivery;
    expect(result.history.filter((e: {
        status: string;
    }) => e.status === 'failed')).toEqual([]);
    return id;
}
export function interaction(action: string, ts: string) {
    return { type: 'block_actions', team: { id: 'T001' }, user: { id: 'U001' }, channel: { id: 'C001' },
        trigger_id: 'trigger_e2e', message: { ts, thread_ts: '100.000001' },
        actions: [{ action_id: action, value: 'tool_1', action_ts: String(Date.now()) }] };
}
export async function completionWebhook(api: APIRequestContext, sessionId: string) {
    const id = `msg_${randomUUID()}`;
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const body = JSON.stringify({ id: `whe_${randomUUID()}`, created_at: new Date().toISOString(), type: 'event',
        data: { id: sessionId, organization_id: 'org_e2e', workspace_id: 'wrkspc_e2e', type: 'session.status_idled' } });
    const sig = createHmac('sha256', 'e2e-webhook-secret').update(`${id}.${timestamp}.${body}`).digest('base64');
    const response = await api.post(`${backend}/anthropic/webhook`, { data: body, headers: { 'content-type': 'application/json', 'webhook-id': id, 'webhook-timestamp': timestamp, 'webhook-signature': `v1,${sig}` } });
    expect(response.status(), await response.text()).toBe(200);
}
