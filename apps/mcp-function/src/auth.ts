import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";

export type AuthConfig = { issuer: string; resource: string; clientIds: string[]; scope: string };
export type Identity = { issuer: string; subject: string };
export type Authenticate = (authorization: string | undefined) => Promise<Identity>;
export class AuthenticationError extends Error {}

export function jwtAuthenticator(config: AuthConfig, keys: JWTVerifyGetKey = createRemoteJWKSet(new URL(`${config.issuer}/.well-known/jwks.json`))): Authenticate {
  return async authorization => {
    const match = /^Bearer ([^\s]+)$/i.exec(authorization || "");
    if (!match || match[1].length > 16384) throw new AuthenticationError();
    try {
      const { payload } = await jwtVerify(match[1], keys, { issuer: config.issuer, audience: config.resource,
        algorithms: ["RS256"], requiredClaims: ["sub", "exp", "client_id", "token_use"] });
      if (payload.token_use !== "access" || typeof payload.client_id !== "string" || !config.clientIds.includes(payload.client_id)
        || typeof payload.scope !== "string" || !payload.scope.split(" ").includes(config.scope) || !payload.sub) throw new AuthenticationError();
      return { issuer: config.issuer, subject: payload.sub };
    } catch { throw new AuthenticationError(); }
  };
}
