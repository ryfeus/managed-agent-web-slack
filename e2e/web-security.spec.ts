import { createHmac, randomUUID } from 'node:crypto';
import { test, expect } from './helpers/fixtures';
import { backend, state } from './helpers/api';

test('Web API preserves metadata routes, cookie expiry and cross-principal isolation', async ({ request }) => {
  const login = await request.post(`${backend}/api/auth/session`, { data: { accessToken: 'e2e-access-token' } });
  expect(login.status()).toBe(200);
  const data = { clientRequestId: randomUUID(), title: 'Metadata test' };
  const created = await request.post(`${backend}/api/threads`, { data });
  expect(created.status()).toBe(201);
  const thread = await created.json();
  expect((await (await request.post(`${backend}/api/threads`, { data })).json()).id).toBe(thread.id);
  expect((await request.get(`${backend}/api/threads/${thread.id}`)).status()).toBe(200);
  await request.post(`${backend}/api/threads/${thread.id}/archive`, { data: {} });
  expect((await (await request.get(`${backend}/api/threads`)).json()).data).toHaveLength(0);
  expect((await request.get(`${backend}/api/agui/threads/${thread.id}/history`)).status()).toBe(404);
  await request.post(`${backend}/api/threads/${thread.id}/restore`, { data: {} });
  expect((await (await request.get(`${backend}/api/threads`)).json()).data).toHaveLength(1);
  const cookie = (principalId: string, expiresAt = Date.now() + 10000) => {
    const value = Buffer.from(JSON.stringify({ principalId, expiresAt })).toString('base64url');
    return `managed_agent_session=${value}.${createHmac('sha256', 'e2e-cookie-secret-at-least-32-bytes').update(value).digest('base64url')}`;
  };
  const foreign = cookie('00000000-0000-4000-8000-000000000002');
  for (const path of [`/api/threads/${thread.id}`, `/api/agui/threads/${thread.id}/history`, `/api/agui/threads/${thread.id}/resume`]) {
    expect((await request.get(backend + path, { headers: { cookie: foreign } })).status()).toBe(404);
  }
  for (const path of [`/api/threads/${thread.id}/archive`, `/api/threads/${thread.id}/restore`, `/api/agui/threads/${thread.id}/cancel`]) {
    expect((await request.post(backend + path, { data: {}, headers: { cookie: foreign } })).status()).toBe(404);
  }
  const input = { threadId: thread.id, runId: randomUUID(), messages: [{ id: randomUUID(), role: 'user', content: 'unauthorized' }], state: {}, tools: [], context: [], forwardedProps: {} };
  expect((await request.post(`${backend}/api/agui`, { data: input, headers: { cookie: foreign } })).status()).toBe(404);
  expect((await request.get(`${backend}/api/threads`, { headers: { cookie: cookie('00000000-0000-4000-8000-000000000001', 0) } })).status()).toBe(401);
  expect((await request.post(`${backend}/api/threads`, { data, headers: { origin: 'https://foreign.test' } })).status()).toBe(403);
  expect((await state(request)).database.agent_tasks).toHaveLength(0);
  expect((await state(request)).a2a.create_calls).toBe(0);
  expect((await request.delete(`${backend}/api/auth/session`)).headers()['set-cookie']).toContain('Max-Age=0');
  expect((await request.get(`${backend}/api/auth/session`)).status()).toBe(401);
});
