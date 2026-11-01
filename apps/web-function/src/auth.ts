import { createHmac, timingSafeEqual } from "node:crypto";

export const COOKIE = "managed_agent_session";
const MAX_AGE = 43200;
export function constantEqual(actual: string, expected: string): boolean {
  const a = Buffer.from(actual), b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}
function signature(payload: string, secret: string) { return createHmac("sha256", secret).update(payload).digest("base64url"); }
export function principal(cookie: string | undefined, secret: string, now = Date.now()): string | undefined {
  if (!secret) return;
  const value = cookie?.split(";").map(p => p.trim()).find(p => p.startsWith(COOKIE + "="))?.slice(COOKIE.length + 1);
  if (!value || value.length > 4096) return;
  const [payload, signed, extra] = value.split(".");
  if (extra !== undefined || !signed || !constantEqual(signed, signature(payload, secret))) return;
  try {
    const data = JSON.parse(Buffer.from(payload, "base64url").toString());
    if (typeof data.principalId === "string" && Number.isSafeInteger(data.expiresAt) && data.expiresAt > now) return data.principalId;
  } catch { /* Untrusted cookie. */ }
}
export function sessionCookie(id: string, secret: string, secure: boolean, clear = false): string {
  const payload = Buffer.from(JSON.stringify({ principalId: id, expiresAt: Date.now() + MAX_AGE * 1000 })).toString("base64url");
  return `${COOKIE}=${clear ? "" : payload + "." + signature(payload, secret)}; Path=/; HttpOnly; SameSite=Strict; ${secure ? "Secure; " : ""}Max-Age=${clear ? 0 : MAX_AGE}`;
}
