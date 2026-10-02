import { test, expect } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { slackTurn, mention, inject, interaction } from './helpers/slack';

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

test('approval controls use A2A request IDs and denial reason reaches provider', async ({ request }) => {
    await control(request, 'a2a/script', { prompt: 'fetch this', events: [
        { id: 'private-tool', type: 'agent.tool_use', name: 'web_fetch', input: { url: 'https://example.test' } },
        { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['private-tool'] } },
    ] });
    await inject(request, mention('fetch this'));
    await drain(request);
    let s = await state(request);
    expect(s.slack.agent_statuses.at(-1).status).toBe('suspended');
    const approval = s.slack.messages.find((m: { blocks: unknown }) => JSON.stringify(m.blocks).includes('agent_tool_allow'));
    expect(approval).toBeTruthy();
    const value = approval.blocks[1].elements[0].value as string;
    expect(JSON.parse(value)).toEqual({ taskId: s.database.agent_tasks[0].task_id, requestId: s.database.cma_input_requests[0].request_id });
    expect(value).not.toContain('private-tool');
    await inject(request, interaction('agent_tool_deny_with_reason', approval.ts, value), true);
    s = await state(request);
    expect(s.slack.modals).toHaveLength(1);
    await inject(request, { type: 'view_submission', team: { id: 'T001' }, user: { id: 'U001' }, view: {
        id: 'view_1', callback_id: 'agent_tool_deny_reason', private_metadata: s.slack.modals[0].view.private_metadata,
        state: { values: { reason: { value: { value: 'Unsafe URL' } } } },
    } }, true);
    await drain(request);
    s = await state(request);
    expect(JSON.stringify(s.a2a.events)).toContain('Unsafe URL');
    expect(s.a2a.confirm_calls).toBe(1);
    expect(s.slack.messages.find((m: { ts: string }) => m.ts === approval.ts).text).toContain('Denied by');
    expect(s.slack.agent_statuses.at(-1).status).toBe('active');
});

test('live append failure recovers from current A2A Task', async ({ request }) => {
    await control(request, 'fail/slack', { operation: 'chat.appendStream', error: 'rate_limited' });
    await slackTurn(request);
    const s = await state(request);
    expect(s.slack.streams).toHaveLength(1);
    expect(s.slack.streams[0]).toMatchObject({ text: 'Hello from Claude', state: 'completed' });
    expect(s.database.slack_projection_items.filter((item: { status: string }) => item.status === 'posted')).toHaveLength(1);
});

test('shortcut submits sanitized context and feedback stores A2A identity', async ({ request }) => {
    await slackTurn(request);
    await inject(request, { type: 'message_action', callback_id: 'summarize_thread',
        team: { id: 'T001' }, user: { id: 'U001' }, channel: { id: 'C001' },
        message: { ts: '1000.000001', thread_ts: '100.000001' } }, true);
    await drain(request);
    let s = await state(request);
    expect(s.database.agent_tasks).toHaveLength(2);
    expect(JSON.stringify(s.a2a.events)).toContain('untrusted reference data');
    const blocks = s.slack.streams[0].blocks;
    const feedback = blocks.find((b: { block_id: string }) => b.block_id.startsWith('feedback:'));
    await inject(request, { type: 'block_actions', team: { id: 'T001' }, user: { id: 'U001' },
        channel: { id: 'C001' }, message: { ts: s.slack.streams[0].ts, thread_ts: '100.000001' },
        actions: [{ action_id: 'agent_feedback', value: 'positive', block_id: feedback.block_id }] }, true);
    await drain(request);
    s = await state(request);
    expect(s.database.thread_feedback).toHaveLength(1);
    expect(s.database.thread_feedback[0].task_id).toBe(s.database.agent_tasks[0].task_id);
    expect(s.database.thread_feedback[0].message_id).toBeTruthy();
});

test('thread linking and unfurl use application thread identity', async ({ request }) => {
    const threadId = await slackTurn(request);
    await inject(request, mention(`link ${threadId}`, { channel: 'C002', ts: '200.000001' }, 'EvLink'));
    await drain(request);
    let s = await state(request);
    expect(s.database.thread_surface_bindings).toHaveLength(2);
    await inject(request, { type: 'event_callback', team_id: 'T001', event_id: 'EvUnfurl', event: {
        type: 'link_shared', user: 'U001', channel: 'C002', message_ts: '200.000002',
        links: [{ url: `http://127.0.0.1:3000/?thread=${threadId}` }],
    } });
    await drain(request);
    s = await state(request);
    expect(s.slack.unfurls).toHaveLength(1);
    expect(JSON.stringify(s.slack.unfurls[0])).toContain('agent_link_thread');
});

test('unknown identity and unowned bound reply cannot send', async ({ request }) => {
    await inject(request, mention('forbidden'), false, false);
    await slackTurn(request);
    await inject(request, mention('forbidden', { user: 'U002', type: 'message', text: 'forbidden',
        thread_ts: '100.000001', ts: '100.000002' }));
    await drain(request);
    const s = await state(request);
    expect(s.database.agent_tasks).toHaveLength(1);
    expect(s.slack.reactions).toHaveLength(1);
    expect(s.slack.messages.at(-1).text).toContain('not authorized');
});
