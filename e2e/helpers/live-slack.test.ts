import { expect, test, vi } from 'vitest';
import { createLiveSlackClient } from './live-slack';

const tokens = { SLACK_BOT_TOKEN: 'xoxb-test-secret', SLACK_USER_TOKEN: 'xoxp-test-secret' };

function api(result: unknown = { ok: true }, status = 200) {
    return { post: vi.fn().mockResolvedValue({
        json: async () => result, ok: () => status < 400, status: () => status,
    }) };
}

test.each(['SLACK_BOT_TOKEN', 'SLACK_USER_TOKEN'] as const)(
    'client rejects missing or blank %s before any Slack request', name => {
        for (const value of [undefined, '   ']) {
            const request = api();
            expect(() => createLiveSlackClient(request, { ...tokens, [name]: value }))
                .toThrow(`Missing ${name}`);
            expect(request.post).not.toHaveBeenCalled();
        }
    },
);

test('bot owns agent operations; user sends human turns and reads channel threads', async () => {
    const request = api();
    const slack = createLiveSlackClient(request, tokens);
    await slack('auth.test', {});
    await slack('chat.postMessage', { channel: 'C1', text: 'fixture anchor' });
    await slack('auth.test', {}, 'user');
    await slack('chat.postMessage', { channel: 'C1', text: '<@U2> hello' }, 'user');
    await slack('conversations.replies', { channel: 'C1', ts: '1.0', limit: 100 });
    expect(request.post.mock.calls.map(([, options]) => options.headers.authorization)).toEqual([
        `Bearer ${tokens.SLACK_BOT_TOKEN}`, `Bearer ${tokens.SLACK_BOT_TOKEN}`,
        `Bearer ${tokens.SLACK_USER_TOKEN}`, `Bearer ${tokens.SLACK_USER_TOKEN}`,
        `Bearer ${tokens.SLACK_USER_TOKEN}`,
    ]);
});

test('API diagnostics redact both tokens', async () => {
    const request = api({ ok: false, error: `bad ${tokens.SLACK_BOT_TOKEN} ${tokens.SLACK_USER_TOKEN}` });
    await expect(createLiveSlackClient(request, tokens)('auth.test', {}))
        .rejects.toThrow('Slack auth.test failed: bad [REDACTED] [REDACTED]');
});

test('transport and JSON diagnostics omit headers, tokens, and response bodies', async () => {
    const request = api();
    request.post.mockRejectedValueOnce(new Error(`headers Bearer ${tokens.SLACK_BOT_TOKEN}`));
    const slack = createLiveSlackClient(request, tokens);
    await expect(slack('auth.test', {})).rejects.toThrow(/^Slack auth.test transport error$/);
    request.post.mockResolvedValueOnce({
        json: async () => { throw new Error(tokens.SLACK_USER_TOKEN); }, status: () => 502,
    });
    await expect(slack('auth.test', {}))
        .rejects.toThrow(/^Slack auth.test returned invalid JSON \(HTTP 502\)$/);
});

test('HTTP failures and malformed results cannot pass as Slack success', async () => {
    for (const result of [null, [], 'unexpected', { ok: false }]) {
        await expect(createLiveSlackClient(api(result), tokens)('auth.test', {}))
            .rejects.toThrow('Slack auth.test failed: unknown_error');
    }
    await expect(createLiveSlackClient(api({ ok: true }, 503), tokens)('auth.test', {}))
        .rejects.toThrow('Slack auth.test failed: HTTP 503');
});

test('an ambiguous post failure is never retried', async () => {
    const request = api();
    request.post.mockRejectedValue(new Error('timeout after write'));
    await expect(createLiveSlackClient(request, tokens)('chat.postMessage', {
        channel: 'C1', text: 'hello',
    }, 'user')).rejects.toThrow('transport error');
    expect(request.post).toHaveBeenCalledTimes(1);
});
