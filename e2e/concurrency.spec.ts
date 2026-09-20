import { test, expect } from './helpers/fixtures';
import { control, state } from './helpers/api';
import { inject, mention, slackTurn } from './helpers/slack';
test('E2E-012 concurrent handlers deliver each input once; sequencing is deferred', async ({ request }, testInfo) => {
    await slackTurn(request);
    for (const key of ['EvA', 'EvB'])
        await control(request, 'faults', { point: 'before_agent_send', key, behavior: 'pause' });
    await inject(request, mention('A', { type: 'message', text: 'A', thread_ts: '100.000001', ts: '100.000002' }, 'EvA'));
    await inject(request, mention('B', { type: 'message', text: 'B', thread_ts: '100.000001', ts: '100.000003' }, 'EvB'));
    const delivery = control(request, 'events/drain', { workers: 2, max_events: 2 });
    await expect.poll(async () => (await state(request)).faults.filter((f: {
        entered: number;
    }) => f.entered === 1).length).toBe(2);
    await control(request, 'faults/release', { point: 'before_agent_send', key: 'EvB' });
    await expect.poll(async () => (await state(request)).agent.sent_messages.length).toBe(2);
    await control(request, 'faults/release', { point: 'before_agent_send', key: 'EvA' });
    await delivery;
    const sent = (await state(request)).agent.sent_messages.slice(1).map((m: {
        text: string;
    }) => m.text);
    expect(sent).toEqual(['B', 'A']);
    testInfo.annotations.push({ type: 'known limitation', description: 'KL-001: Concurrent handlers have no production per-session sequencer. This controlled B-before-A interleaving documents that gap.' });
    await testInfo.attach('observed-input-order', { body: JSON.stringify(sent), contentType: 'application/json' });
});

test('reset releases an in-flight handler and clears its final mutations', async ({ request }) => {
    await slackTurn(request);
    await control(request, 'faults', { point: 'before_agent_send', key: 'EvReset', behavior: 'pause' });
    await inject(request, mention('reset me', { type: 'message', text: 'reset me', thread_ts: '100.000001', ts: '100.000002' }, 'EvReset'));
    const delivery = control(request, 'events/drain');
    await expect.poll(async () => (await state(request)).faults[0].entered).toBe(1);
    await control(request, 'reset');
    await delivery;
    const cleared = await state(request);
    expect(cleared.agent.sent_messages).toEqual([]);
    expect(cleared.agent.sessions).toEqual([]);
    expect(cleared.slack.messages).toEqual([]);
    expect(cleared.events).toEqual({ pending: [], history: [] });
    expect(cleared.database.agent_sessions).toEqual([]);
});
