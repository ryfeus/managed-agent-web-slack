import type { APIRequestContext } from '@playwright/test';

type SlackActor = 'bot' | 'user';
type SlackEnvironment = { SLACK_BOT_TOKEN?: string; SLACK_USER_TOKEN?: string };

/** API-only sandbox access: user reads channel threads; bot owns agent operations. */
export function createLiveSlackClient(request: Pick<APIRequestContext, 'post'>, env: SlackEnvironment) {
    const tokens = { bot: env.SLACK_BOT_TOKEN?.trim(), user: env.SLACK_USER_TOKEN?.trim() };
    for (const [actor, token] of Object.entries(tokens)) {
        if (!token) throw new Error(`Missing SLACK_${actor.toUpperCase()}_TOKEN`);
    }
    function safeError(value: unknown): string {
        let text = String(value);
        for (const token of Object.values(tokens)) text = text.replaceAll(token!, '[REDACTED]');
        return text.slice(0, 100);
    }
    return async function slack(
        method: string,
        data: Record<string, string | number | boolean>,
        actor: SlackActor = method === 'conversations.replies' ? 'user' : 'bot',
    ): Promise<any> {
        let response;
        try {
            response = await request.post(`https://slack.com/api/${method}`, {
                headers: { authorization: `Bearer ${tokens[actor]}` }, form: data,
            });
        } catch {
            // Playwright transport errors can include request headers and tokens.
            throw new Error(`Slack ${method} transport error`);
        }
        let result;
        try {
            result = await response.json();
        } catch {
            throw new Error(`Slack ${method} returned invalid JSON (HTTP ${response.status()})`);
        }
        if (!result || result.ok !== true) {
            throw new Error(`Slack ${method} failed: ${safeError(result?.error ?? 'unknown_error')}`);
        }
        if (!response.ok()) throw new Error(`Slack ${method} failed: HTTP ${response.status()}`);
        return result;
    };
}
