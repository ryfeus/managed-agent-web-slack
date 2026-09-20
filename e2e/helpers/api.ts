import { expect, type APIRequestContext } from '@playwright/test';
export const backend = 'http://127.0.0.1:3001';
export async function control(api: APIRequestContext, path: string, data: object = {}) {
    const response = await api.post(`${backend}/_test/${path}`, { data, timeout: 30000 });
    expect(response.ok(), await response.text()).toBeTruthy();
    return response.json();
}
export async function state(api: APIRequestContext) {
    const response = await api.get(`${backend}/_test/state`);
    expect(response.ok()).toBeTruthy();
    return response.json();
}
export const answerEvents = (text: string) => [
    { type: 'event_start', event: { id: 'answer_final', type: 'agent.message' } },
    { type: 'event_delta', event_id: 'answer_final', delta: { content: { type: 'text', text } } },
    { id: 'answer_final', type: 'agent.message', content: [{ type: 'text', text }] },
    { type: 'session.status_idle', stop_reason: { type: 'end_turn' } },
];
export async function ready(api: APIRequestContext, sessionId?: string, count = 1) {
    await expect.poll(async () => {
        const s = await state(api);
        return sessionId ? s.agent.subscribers[sessionId] || 0 : Object.values<number>(s.agent.subscribers).reduce((a, b) => a + b, 0);
    }).toBeGreaterThanOrEqual(count);
}
export async function drain(api: APIRequestContext) {
    const result = await control(api, 'events/drain');
    expect(result.pending).toEqual([]);
    expect(result.history.filter((e: {
        status: string;
    }) => e.status === 'failed')).toEqual([]);
    return result;
}
