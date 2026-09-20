import { test, expect } from './helpers/fixtures';
import { control, drain, state } from './helpers/api';
import { inject, mention } from './helpers/slack';

test('SQS-E2E-001 and SQS-E2E-002 queue accepted input and deduplicate a duplicate delivery', async ({ request }) => {
    const event = mention('queued once', {}, 'EvSqsDuplicate');
    await inject(request, event);
    await inject(request, event);
    await drain(request);
    const result = await state(request);
    expect(result.agent.sent_messages).toHaveLength(1);
    expect(result.queue.history.filter((item: { status: string }) => item.status === 'completed')).toHaveLength(2);
});

test('SQS-E2E-003 retries a before-send failure after deterministic visibility expiry', async ({ request }) => {
    await control(request, 'faults', { point: 'before_agent_send', key: 'EvSqsBefore', times: 1 });
    await inject(request, mention('retry before send', {}, 'EvSqsBefore'));
    await control(request, 'events/drain');
    let result = await state(request);
    expect(result.agent.sent_messages).toEqual([]);
    expect(result.queue.messages[0]).toMatchObject({ receive_count: 1, state: 'inflight' });
    await control(request, 'queue/advance', { seconds: 360 });
    await drain(request);
    result = await state(request);
    expect(result.agent.sent_messages).toHaveLength(1);
    expect(result.queue.messages[0]).toMatchObject({ receive_count: 2, state: 'deleted' });
});

test('SQS-E2E-004 documents the post-send duplicate crash window', async ({ request }, testInfo) => {
    await control(request, 'faults', { point: 'after_agent_send', key: 'EvSqsAfter', times: 1 });
    await inject(request, mention('retry after send', {}, 'EvSqsAfter'));
    await control(request, 'events/drain');
    expect((await state(request)).agent.sent_messages).toHaveLength(1);
    await control(request, 'queue/advance', { seconds: 360 });
    await drain(request);
    expect((await state(request)).agent.sent_messages).toHaveLength(2);
    testInfo.annotations.push({ type: 'known limitation', description: 'KL-002: A crash after Anthropic accepts an input but before DSQL marks it sent can redeliver that input.' });
});

test('SQS-E2E-005 poison records reach the local processing DLQ without blocking unrelated input', async ({ request }) => {
    await control(request, 'queue/send-raw', { body: 'malformed-json' });
    for (let attempt = 0; attempt < 5; attempt += 1) {
        await control(request, 'queue/drain');
        await control(request, 'queue/advance', { seconds: 360 });
    }
    await control(request, 'queue/drain');
    const poisoned = await state(request);
    expect(poisoned.queue.messages[0]).toMatchObject({ receive_count: 5, state: 'dlq' });
    await inject(request, mention('unrelated succeeds', {}, 'EvSqsUnrelated'));
    await drain(request);
    expect((await state(request)).agent.sent_messages).toHaveLength(1);
});
