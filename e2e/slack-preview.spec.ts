import { test, expect, login } from './helpers/fixtures';
import { control, state, drain, backend } from './helpers/api';
import { inject, mention } from './helpers/slack';
import type { APIRequestContext } from '@playwright/test';

const previewState = async (request: APIRequestContext) => (await request.get(`${backend}/_test/preview/state`)).json();
const readers = async (request: APIRequestContext) => (await request.get(`${backend}/_test/preview-readers`)).json();
const emit = (request: APIRequestContext, session: string, event: object) => control(request, 'a2a/emit', { session_id: session, event });
async function start(request: APIRequestContext, enabled = true) {
  await control(request, 'a2a/automatic', { enabled: false });
  await control(request, 'preview-control', { enabled: true });
  await control(request, 'slack-preview-control', { enabled });
  await inject(request, mention('one shared execution', {}, 'EvPreview'));
  await drain(request);
  await expect.poll(async () => (await readers(request)).connected).toBe(1);
  return (await state(request)).a2a.sessions[0].id as string;
}
async function draft(request: APIRequestContext, session: string) {
  await emit(request, session, { type: 'event_start', event: { type: 'agent.message', id: 'preview-message' } });
  await emit(request, session, { type: 'event_delta', event_id: 'preview-message', delta: { type: 'content_delta', index: 0, content: { type: 'text', text: 'Provisional wording' } } });
}
async function finish(request: APIRequestContext, session: string) {
  await emit(request, session, { id: 'preview-message', type: 'agent.message', content: [{ type: 'text', text: 'Exact canonical answer.' }] });
  await emit(request, session, { type: 'session.status_idle', stop_reason: { type: 'end_turn' } });
  await control(request, 'a2a/reconcile', { session_id: session });
  await drain(request);
}
const draftCalls = (s: any) => s.slack.calls.filter((c: any) => c.operation === 'chat.appendStream' && c.payload.chunks?.some((chunk: any) => chunk.title?.startsWith('Draft:')));
test.beforeEach(async ({ request }) => {
  await control(request, 'preview/config', { failPublish: false, drop: false, duplicate: false, rejectSubscribe: false });
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(0);
});

test('Slack and Web share one CMA reader and AppSync channel; only canonical Markdown survives', async ({ page, request }) => {
  const session = await start(request), initial = await state(request);
  await login(page);
  await page.goto(`/?thread=${initial.database.agent_threads[0].thread_id}`);
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(2);
  await control(request, 'preview/config', { duplicate: true });
  await draft(request, session);
  await expect.poll(async () => (await state(request)).slack.streams[0].tasks['request:EvPreview'].title).toBe('Draft: …Provisional wording');
  await expect(page.locator('.assistant-message')).toContainText('Provisional wording');
  const running = await state(request);
  expect(running.slack.streams[0].text).toBe('');
  expect(draftCalls(running)).toHaveLength(1);
  expect(JSON.stringify(running.database)).not.toContain('Provisional wording');
  expect((await readers(request)).started).toBe(1);
  await expect.poll(async () => (await state(request)).database.slack_task_projections[0].claim_id).toBeNull();
  // Missing end marker is harmless: canonical push settles both consumers.
  await control(request, 'preview/config', { drop: true });
  await finish(request, session);
  const final = await state(request);
  expect(final.slack.streams).toHaveLength(1);
  expect(final.slack.streams[0]).toMatchObject({ text: 'Exact canonical answer.', state: 'completed', tasks: { 'request:EvPreview': { title: 'Request complete' } } });
  expect(final.database.slack_projection_items).toHaveLength(1);
  expect(final.database.agent_tasks).toHaveLength(1);
  expect(final.a2a.create_calls).toBe(1); expect(final.a2a.confirm_calls).toBe(0);
  await expect(page.locator('.assistant-message')).toHaveCount(1);
  await expect(page.locator('.assistant-message')).toContainText('Exact canonical answer.');
  await expect(page.locator('.assistant-message')).not.toContainText('Provisional');
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(0);
});

for (const failure of ['rate_limited', 'ambiguous', 'closed', 'appsync', 'disabled', 'no-stream']) {
  test(`Slack preview ${failure} preserves one canonical answer`, async ({ request }) => {
    if (failure === 'appsync') await control(request, 'preview/config', { rejectSubscribe: true });
    if (failure === 'no-stream') for (let i = 0; i < 8; i++) await control(request, 'fail/slack', { operation: 'chat.startStream', error: 'missing_scope' });
    const session = await start(request, failure !== 'disabled');
    const hasPreview = ['rate_limited', 'ambiguous', 'closed'].includes(failure);
    if (hasPreview) {
      await expect.poll(async () => (await previewState(request)).subscribers).toBe(1);
      await control(request, 'fail/slack', { operation: 'chat.appendStream', error: failure === 'closed' ? 'message_not_in_streaming_state' : 'rate_limited', after: failure === 'ambiguous' });
    }
    await draft(request, session);
    if (hasPreview) await expect.poll(async () => draftCalls(await state(request)).length).toBe(1);
    await expect.poll(async () => (await previewState(request)).subscribers).toBe(0);
    await finish(request, session);
    const final = await state(request);
    expect(final.slack.messages.filter((m: any) => m.text === 'Exact canonical answer.')).toHaveLength(1);
    expect(final.a2a.create_calls).toBe(1); expect(final.database.agent_tasks).toHaveLength(1);
    if (!hasPreview) expect(draftCalls(final)).toHaveLength(0);
    if (final.slack.streams.length) expect(final.slack.streams[0].tasks['request:EvPreview'].title).toBe('Request complete');
  });
}

test('human input closes the draft card; resumed canonical reply never reopens it', async ({ request }) => {
  const session = await start(request);
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(1);
  await draft(request, session);
  await expect.poll(async () => draftCalls(await state(request)).length).toBe(1);
  await expect.poll(async () => (await state(request)).database.slack_task_projections[0].claim_id).toBeNull();
  await control(request, 'policy', { fixture: 'human-review' });
  await emit(request, session, { id: 'review-tool', type: 'agent.tool_use', name: 'web_search', input: { query: 'exact review' } });
  await emit(request, session, { type: 'session.status_idle', stop_reason: { type: 'requires_action', event_ids: ['review-tool'] } });
  await control(request, 'a2a/reconcile', { session_id: session }); await drain(request);
  const waiting = await state(request);
  expect(waiting.slack.streams[0]).toMatchObject({ state: 'completed', status: 'suspended', text: '', tasks: { 'request:EvPreview': { title: 'Waiting for your approval' } } });
  expect(JSON.stringify(waiting.slack.messages)).toContain('agent_tool_allow');
  expect(waiting.a2a.confirm_calls).toBe(0);
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(0);
  // Signed ruling retains native OpenAPPA authority and the P0 reply path.
  const message = waiting.slack.messages.find((m: any) => JSON.stringify(m.blocks).includes('agent_tool_allow'));
  const button = message.blocks.find((b: any) => b.type === 'actions').elements[0];
  await inject(request, { type: 'block_actions', team: { id: 'T001' }, user: { id: 'U001' }, channel: { id: 'C001' },
    message: { ts: message.ts, thread_ts: '100.000001' }, actions: [{ action_id: button.action_id, value: button.value, action_ts: String(Date.now()) }] }, true);
  await drain(request);
  await finish(request, session);
  const final = await state(request);
  expect(final.a2a.confirm_calls).toBe(1); expect(final.slack.streams).toHaveLength(1);
  expect(final.slack.messages.filter((m: any) => m.text === 'Exact canonical answer.')).toHaveLength(1);
});
