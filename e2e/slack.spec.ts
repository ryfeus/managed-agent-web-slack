import { test, expect } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { slackTurn, mention, inject } from './helpers/slack';

test('Slack A2A turn creates one application thread and task card', async ({ request }) => {
    await slackTurn(request);
    const s = await state(request);
    expect(s.database.agent_threads).toHaveLength(1);
    expect(s.database.thread_surface_bindings).toHaveLength(1);
    expect(s.database.agent_tasks).toHaveLength(1);
    expect(s.a2a.create_calls).toBe(1);
    expect(s.slack.streams).toHaveLength(1);
    expect(s.slack.streams[0]).toMatchObject({ text: 'Hello from Claude', state: 'completed', status: 'active', mode: 'chunks' });
    expect(s.slack.streams[0].chunks[0].sources[0].url).toContain('p100000001');
    expect(s.slack.reactions).toEqual([{ channel: 'C001', timestamp: '100.000001', name: 'eyes' }]);
});

test('duplicate signed Slack delivery admits one A2A Task', async ({ request }) => {
    const event = mention('hello', {}, 'EvDuplicate');
    await inject(request, event);
    await inject(request, event);
    await drain(request);
    const s = await state(request);
    expect(s.database.surface_ingress_events).toHaveLength(1);
    expect(s.database.agent_tasks).toHaveLength(1);
    expect(s.a2a.create_calls).toBe(1);
    expect(s.slack.streams).toHaveLength(1);
});

test('bound reply reuses context with its own source receipt', async ({ request }) => {
    await slackTurn(request);
    await slackTurn(request, 'next', 'Second answer', {
        type: 'message', text: 'next', thread_ts: '100.000001', ts: '100.000002',
    });
    const s = await state(request);
    expect(s.database.agent_threads).toHaveLength(1);
    expect(s.database.cma_contexts).toHaveLength(1);
    expect(s.database.agent_tasks).toHaveLength(2);
    expect(s.slack.reactions.map((r: { timestamp: string }) => r.timestamp)).toEqual(['100.000001', '100.000002']);
    expect(s.slack.streams[1].text).toBe('Second answer');
    expect(s.slack.streams[1].chunks[0].sources[0].url).toContain('p100000002');
});

test('OpenAPPA decisions complete Slack tools without approval controls', async ({ request }) => {
    await control(request, 'a2a/script', { prompt: 'fetch this', events: [
        { id: 'private-tool', type: 'agent.tool_use', name: 'web_fetch', input: { url: 'https://example.test' } },
        { id: 'denied-tool', type: 'agent.tool_use', name: 'unknown', input: {} },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['private-tool', 'denied-tool'] } },
    ] });
    await inject(request, mention('fetch this'));
    await expect.poll(async () => { await drain(request); return (await state(request)).database.cma_tasks[0]?.a2a_state; }).toBe('COMPLETED');
    const s = await state(request);
    expect(s.a2a.confirm_calls).toBe(2);
    expect(s.database.cma_input_requests.map((r: {decision: string}) => r.decision).sort()).toEqual(['allow','deny']);
    expect(JSON.stringify(s.slack.messages)).not.toContain('agent_tool_allow');
    expect(JSON.stringify(s.a2a.events)).toContain('Denied by OpenAPPA policy.');
    expect(s.slack.agent_statuses.at(-1).status).toBe('active');
});

test('push append failure retries current A2A Task without resubmitting input', async ({ request }) => {
    await control(request, 'fail/slack', { operation: 'chat.appendStream', error: 'rate_limited' });
    await control(request, 'a2a/script', { prompt: 'hello', response: 'Hello from Claude' });
    await inject(request, mention('hello'));
    const deliveries = await control(request, 'events/drain');
    const failed = deliveries.history.filter((event: { status: string }) => event.status === 'failed');
    expect(failed).toHaveLength(1);
    expect(failed[0].error).toContain('rate_limited');
    await control(request, 'events/retry', { id: failed[0].id });
    await drain(request);
    const s = await state(request);
    expect(s.slack.streams).toHaveLength(1);
    expect(s.slack.streams[0]).toMatchObject({ text: 'Hello from Claude', state: 'completed' });
    expect(s.database.slack_projection_items.filter((item: { status: string }) => item.status === 'posted')).toHaveLength(1);
    expect(s.a2a.create_calls).toBe(1);
    expect(s.database.agent_tasks).toHaveLength(1);
});

test('explicit linking uses application thread identity', async ({ request }) => {
    const threadId = await slackTurn(request);
    await inject(request, mention(`link ${threadId}`, { channel: 'C002', ts: '200.000001' }, 'EvLink'));
    await drain(request);
    const s = await state(request);
    expect(s.database.thread_surface_bindings).toHaveLength(2);

});

test('unknown identity and unowned bound reply cannot send', async ({ request }) => {
    await inject(request, mention('forbidden'), false, false);
    await inject(request, mention('unmapped user', { user: 'U_UNKNOWN' }, 'EvUnknownIdentity'));
    await drain(request);
    const rejected = await state(request);
    expect(rejected.database.agent_threads).toHaveLength(0);
    expect(rejected.database.agent_tasks).toHaveLength(0);
    expect(rejected.a2a.create_calls).toBe(0);
    expect(rejected.a2a.send_calls).toBe(0);
    expect(rejected.slack.reactions).toHaveLength(0);
    expect(rejected.slack.messages.at(-1).text).toContain('not linked to an application principal');
    await slackTurn(request);
    await inject(request, mention('forbidden', { user: 'U002', type: 'message', text: 'forbidden',
        thread_ts: '100.000001', ts: '100.000002' }));
    await drain(request);
    const s = await state(request);
    expect(s.database.agent_tasks).toHaveLength(1);
    expect(s.slack.reactions).toHaveLength(1);
    expect(s.slack.messages.at(-1).text).toContain('not authorized');
});
