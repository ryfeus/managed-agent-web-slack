import { test, expect } from './helpers/fixtures';
import { control, state, drain } from './helpers/api';
import { inject, mention, slackTurn } from './helpers/slack';

test('concurrent duplicate deliveries converge on one A2A Task', async ({ request }) => {
    const event = mention('duplicate', {}, 'EvConcurrent');
    await inject(request, event);
    await inject(request, event);
    await control(request, 'events/drain', { workers: 2 });
    await control(request, 'queue/advance', { seconds: 360 });
    await drain(request);
    const s = await state(request);
    expect(s.database.surface_ingress_events).toHaveLength(1);
    expect(s.database.agent_threads).toHaveLength(1);
    expect(s.database.agent_tasks).toHaveLength(1);
    expect(s.a2a.create_calls).toBe(1);
});

test('reset clears Slack A2A projection and provider state', async ({ request }) => {
    await slackTurn(request);
    await control(request, 'reset');
    const s = await state(request);
    expect(s.a2a.sessions).toEqual([]);
    expect(s.slack.messages).toEqual([]);
    expect(s.events).toEqual({ pending: [], history: [] });
    expect(s.database.agent_threads).toEqual([]);
    expect(s.database.slack_task_projections).toEqual([]);
});
