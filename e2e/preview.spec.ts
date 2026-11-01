import { test, expect, login, send } from './helpers/fixtures';
import { control, state, drain, backend } from './helpers/api';
import type { APIRequestContext } from '@playwright/test';

const previewState = async (request: APIRequestContext) => (await request.get(`${backend}/_test/preview/state`)).json();
const readers = async (request: APIRequestContext) => (await request.get(`${backend}/_test/preview-readers`)).json();
async function emit(request: APIRequestContext, session: string, event: object) {
  await control(request, 'a2a/emit', { session_id: session, event });
}
function delta(text: string, id = 'preview-message') {
  return { type: 'event_delta', event_id: id, delta: { type: 'content_delta', index: 0, content: { type: 'text', text } } };
}

test.beforeEach(async ({ request }) => {
  await control(request, 'preview/config', { failPublish: false, drop: false, duplicate: false, rejectSubscribe: false });
});

test('P1 two viewers share one observer; duplicate previews reconcile to one exact canonical answer', async ({ page, context, request }) => {
  await control(request, 'a2a/automatic', { enabled: false });
  await control(request, 'preview-control', { enabled: true });
  await control(request, 'preview/config', { duplicate: true });
  await login(page); await send(page, 'stream one execution');
  await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
  await expect.poll(async () => (await readers(request)).connected).toBe(1);
  const current = await state(request), session = current.a2a.sessions[0].id, thread = current.database.agent_threads[0].thread_id;
  const second = await context.newPage(); await second.goto(`/?thread=${thread}`);
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(2);
  await emit(request, session, { type: 'event_start', event: { type: 'agent.thinking', id: 'private-thinking' } });
  await emit(request, session, delta('SECRET_REASONING', 'private-thinking'));
  await emit(request, session, { type: 'event_start', event: { type: 'agent.message', id: 'preview-message' } });
  await emit(request, session, delta('Provisional '));
  await expect(page.locator('.assistant-message')).toContainText('Provisional');
  await expect(second.locator('.assistant-message')).toContainText('Provisional');
  await emit(request, session, delta('wording'));
  await expect(page.locator('.assistant-message')).toContainText('Provisional wording');
  expect(await page.locator('body').innerText()).not.toContain('SECRET_REASONING');
  expect((await readers(request)).started).toBe(1);
  // Lose the preview's end marker entirely. Only P0 completion counts.
  await control(request, 'preview/config', { drop: true });
  await emit(request, session, { id: 'preview-message', type: 'agent.message', content: [{ type: 'text', text: 'Exact canonical answer.' }] });
  await emit(request, session, { type: 'session.status_idle', stop_reason: { type: 'end_turn' } });
  await control(request, 'a2a/reconcile', { session_id: session });
  for (const viewer of [page, second]) {
    await expect(viewer.locator('.assistant-message')).toHaveCount(1);
    await expect(viewer.locator('.assistant-message')).toContainText('Exact canonical answer.');
    await expect(viewer.locator('.assistant-message')).not.toContainText('Provisional');
    await expect(viewer.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
  }
  expect((await state(request)).a2a.create_calls).toBe(1);
  expect((await state(request)).database.agent_tasks).toHaveLength(1);
  await expect.poll(async () => (await previewState(request)).subscribers).toBe(0);
  await expect.poll(async () => (await readers(request)).connected).toBe(0);
  await second.close();
});

test('P1 publish outage and missed first-turn deltas preserve canonical completion and disconnect is not Stop', async ({ page, request }) => {
  await control(request, 'preview/config', { failPublish: true });
  await control(request, 'a2a/automatic', { enabled: false });
  await control(request, 'preview-control', { enabled: true });
  await login(page); await send(page, 'outage');
  await expect.poll(async () => { await drain(request); return (await state(request)).a2a.create_calls; }).toBe(1);
  const session = (await state(request)).a2a.sessions[0].id;
  await expect.poll(async () => (await readers(request)).connected).toBe(1);
  // Missing event_start models a connection made after CMA started the message.
  await emit(request, session, delta('missed fragment'));
  await emit(request, session, { type: 'event_start', event: { id: 'later', type: 'agent.message' } });
  await emit(request, session, delta('unavailable', 'later'));
  await expect.poll(async () => (await readers(request)).connected).toBe(0);
  await page.reload();
  expect((await state(request)).a2a.interrupt_calls).toBe(0);
  await emit(request, session, { id: 'later', type: 'agent.message', content: [{ type: 'text', text: 'Canonical survives outage.' }] });
  await emit(request, session, { type: 'session.status_idle', stop_reason: { type: 'end_turn' } });
  await control(request, 'a2a/reconcile', { session_id: session });
  await expect(page.locator('.assistant-message')).toHaveCount(1);
  await expect(page.locator('.assistant-message')).toContainText('Canonical survives outage.');
  expect((await state(request)).a2a.create_calls).toBe(1);
  expect((await state(request)).a2a.send_calls).toBe(0);
});
