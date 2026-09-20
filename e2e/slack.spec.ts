import { test, expect } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { slackTurn, mention, inject, interaction, completionWebhook } from './helpers/slack';
test('E2E-002 Slack task lifecycle and exact source receipt', async ({ request }) => {
    await slackTurn(request);
    const s = await state(request);
    expect(s.agent.sent_messages).toHaveLength(1);
    expect(s.slack.streams).toHaveLength(1);
    expect(s.slack.streams[0]).toMatchObject({ text: 'Hello from Claude', state: 'completed', status: 'active', mode: 'chunks' });
    expect(s.slack.streams[0].chunks[0].sources[0].url).toContain('p100000001');
    expect(s.slack.streams[0].chunks.at(-1).status).toBe('complete');
    expect(s.slack.reactions).toEqual([{ channel: 'C001', timestamp: '100.000001', name: 'eyes' }]);
    expect(s.slack.agent_statuses[0].status).toBe('processing');
    expect(s.slack.agent_statuses.at(-1).status).toBe('active');
});
test('E2E-003 duplicate Slack delivery only sends and projects once', async ({ request }) => {
    const event = mention('hello', {}, 'EvDuplicate');
    await inject(request, event);
    await inject(request, event);
    await drain(request);
    const s = await state(request);
    expect(s.agent.sent_messages).toHaveLength(1);
    // One final response, regardless of whether completion preceded subscription.
    expect(s.slack.messages.filter((m: {
        text: string;
    }) => m.text === 'Echo: hello')).toHaveLength(1);
});
test('E2E-004 bound unmentioned replies reuse session with a new receipt', async ({ request }) => {
    const id = await slackTurn(request);
    await slackTurn(request, 'next', 'Second answer', { type: 'message', text: 'next', thread_ts: '100.000001', ts: '100.000002' });
    const s = await state(request);
    expect(s.agent.sessions).toHaveLength(1);
    expect(s.agent.sent_messages.map((m: {
        session_id: string;
    }) => m.session_id)).toEqual([id, id]);
    expect(s.slack.reactions.map((r: {
        timestamp: string;
    }) => r.timestamp)).toEqual(['100.000001', '100.000002']);
    expect(s.slack.streams[1].thread_ts).toBe('100.000001');
    expect(s.slack.streams[1].chunks[0].sources[0].url).toContain('p100000002');
});
for (const approved of [true, false])
    test(`E2E-00${approved ? 7 : 8} approval ${approved ? 'Allow' : 'Deny with reason'}`, async ({ request }) => {
        await control(request, 'agent/script', { prompt: 'fetch this', events: [
                { id: 'tool_1', type: 'agent.tool_use', name: 'web_fetch', input: { url: 'https://example.test' } },
                { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['tool_1'] } }
            ] });
        await inject(request, mention('fetch this'));
        await drain(request);
        let s = await state(request);
        expect(s.slack.agent_statuses.at(-1).status).toBe('suspended');
        expect(s.slack.streams[0].chunks.some((c: {
            status: string;
        }) => c.status === 'complete')).toBe(false);
        const approval = s.slack.messages.find((m: {
            blocks: unknown;
        }) => JSON.stringify(m.blocks).includes('agent_tool_allow'));
        expect(approval).toBeTruthy();
        if (approved)
            await inject(request, interaction('agent_tool_allow', approval.ts), true);
        else {
            await inject(request, interaction('agent_tool_deny_with_reason', approval.ts), true);
            s = await state(request);
            expect(s.slack.modals).toHaveLength(1);
            await inject(request, { type: 'view_submission', team: { id: 'T001' }, user: { id: 'U001' }, view: { id: 'view_1', callback_id: 'agent_tool_deny_reason', private_metadata: s.slack.modals[0].view.private_metadata, state: { values: { reason: { value: { value: 'Do not access that URL' } } } } } }, true);
        }
        await drain(request);
        s = await state(request);
        expect(s.agent.confirmations).toHaveLength(1);
        expect(s.agent.confirmations[0]).toMatchObject({ approved, ...(!approved ? { reason: 'Do not access that URL' } : {}) });
        expect(s.slack.messages.find((m: {
            ts: string;
        }) => m.ts === approval.ts).blocks || []).toEqual([]);
        expect(s.slack.agent_statuses.at(-1).status).toBe('active');
        expect(s.slack.messages.some((m: {
            text: string;
        }) => m.text.includes(approved ? 'Done' : 'Denied:'))).toBe(true);
    });
for (const error of ['simulated crash', 'rate_limited'])
    test(`E2E-${error === 'rate_limited' ? '011' : '010'} append failure recovers original task`, async ({ request }) => {
        await control(request, 'fail/slack', { operation: 'chat.appendStream', error });
        const id = await slackTurn(request);
        await completionWebhook(request, id);
        await drain(request);
        const s = await state(request);
        expect(s.agent.sent_messages).toHaveLength(1);
        expect(s.slack.messages).toHaveLength(1);
        expect(s.slack.streams[0]).toMatchObject({ text: 'Hello from Claude', state: 'completed' });
        expect(s.database.projection_events[0]).toMatchObject({ status: 'posted', slack_message_ts: s.slack.streams[0].ts });
    });
test('invalid signatures and unknown identities cannot reach the agent', async ({ request }) => {
    await inject(request, mention('forbidden'), false, false);
    await inject(request, mention('forbidden', { user: 'UUNKNOWN' }));
    await drain(request);
    const s = await state(request);
    expect(s.agent.sent_messages).toEqual([]);
    expect(s.slack.reactions).toEqual([]);
});
test('receipt and source-link failures do not block authorized execution', async ({ request }) => {
    await control(request, 'fail/slack', { operation: 'reactions.add' });
    await control(request, 'fail/slack', { operation: 'chat.getPermalink' });
    await inject(request, mention('nonfatal'));
    await control(request, 'events/drain');
    const s = await state(request);
    expect(s.agent.sent_messages).toHaveLength(1);
    const failed = s.events.history.find((e: {
        status: string;
    }) => e.status === 'failed');
    expect(failed['detail-type']).toBe('SlackWorkStarted');
    await control(request, 'events/retry', { id: failed.id });
    await drain(request);
    expect((await state(request)).slack.reactions).toHaveLength(1);
});
test('a mapped identity cannot continue another principal’s binding', async ({ request }) => {
    await slackTurn(request);
    await inject(request, mention('forbidden', { user: 'U002', type: 'message', text: 'forbidden', thread_ts: '100.000001', ts: '100.000002' }));
    await drain(request);
    const s = await state(request);
    expect(s.agent.sent_messages).toHaveLength(1);
    expect(s.slack.reactions).toHaveLength(1);
    expect(s.slack.messages.at(-1).text).toContain('not authorized');
});
test('canonical completion before subscription does not hang or duplicate', async ({ request }) => {
    await inject(request, mention('already finished'));
    await drain(request);
    const s = await state(request);
    expect(s.slack.messages).toHaveLength(1);
    expect(s.slack.streams[0].text).toBe('Echo: already finished');
    expect(Object.values(s.agent.subscribers)).toEqual([0]);
});
test('active stream lease defers completion webhook until live finalization', async ({ request }) => {
    await control(request, 'agent/script', { prompt: 'lease', automatic: false, events: [
            { type: 'agent.message', content: [{ type: 'text', text: 'Lease answer' }] },
            { type: 'session.status_idle', stop_reason: { type: 'end_turn' } }
        ] });
    await inject(request, mention('lease'));
    const delivery = control(request, 'events/drain');
    await expect.poll(async () => (await state(request)).slack.streams.length).toBe(1);
    const id = (await state(request)).agent.sessions[0].id;
    await completionWebhook(request, id);
    // The bus's serial worker is currently in the live projector. The database
    // lease invariant itself is also checked with concurrent real SQL in pytest.
    expect((await state(request)).slack.streams[0].state).toBe('streaming');
    await control(request, 'agent/advance', { session_id: id });
    await delivery;
    expect((await state(request)).slack.messages).toHaveLength(1);
});
