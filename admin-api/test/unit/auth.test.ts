import { createLocalJWKSet, exportJWK, generateKeyPair, type JWTPayload, SignJWT } from "jose";
import { beforeAll, describe, expect, it } from "vitest";
import {
  AuthError,
  bearerToken,
  createVerifier,
  rolesFrom,
  type Verifier,
} from "../../src/auth.js";
import { loadConfig } from "../../src/config.js";

const ISS = "https://idp.test/realms/mcp";
const config = loadConfig({ ADMIN_OIDC_ISSUER: ISS });
let sign: (
  claims: JWTPayload,
  opts?: { alg?: string; exp?: string; key?: CryptoKey },
) => Promise<string>;
let verify: Verifier;
let otherKey: CryptoKey;

beforeAll(async () => {
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  otherKey = (await generateKeyPair("RS256")).privateKey;
  const jwk = { ...(await exportJWK(publicKey)), kid: "k1", alg: "RS256", use: "sig" };
  verify = createVerifier(config, createLocalJWKSet({ keys: [jwk] }));
  sign = (claims, opts = {}) =>
    new SignJWT(claims)
      .setProtectedHeader({ alg: opts.alg ?? "RS256", kid: "k1" })
      .setIssuedAt()
      .setExpirationTime(opts.exp ?? "5m")
      .sign(opts.key ?? privateKey);
});

const admin = {
  iss: ISS,
  aud: ["mcp-admin"],
  sub: "u-1",
  preferred_username: "admin1",
  realm_access: { roles: ["mcp-admin"] },
};

async function rejection(p: Promise<unknown>): Promise<AuthError> {
  const err = await p.then(
    () => undefined,
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(AuthError);
  return err as AuthError;
}

describe("createVerifier", () => {
  it("accepts an admin token and returns the principal", async () => {
    const p = await verify(await sign(admin));
    expect(p).toMatchObject({ sub: "u-1", name: "admin1", roles: ["mcp-admin"] });
  });

  it("403 for a valid token without the role", async () => {
    const err = await rejection(
      verify(await sign({ ...admin, realm_access: { roles: ["mcp-developer"] } })),
    );
    expect([err.status, err.code]).toEqual([403, "insufficient_role"]);
  });

  it.each([
    ["wrong audience", { ...admin, aud: "mcp-gateway" }],
    ["no audience", { ...admin, aud: undefined }],
    ["wrong issuer", { ...admin, iss: "https://evil.test/realms/mcp" }],
    ["no sub", { ...admin, sub: undefined }],
  ])("401 for %s", async (_name, claims) => {
    const err = await rejection(verify(await sign(claims as JWTPayload)));
    expect([err.status, err.code]).toEqual([401, "invalid_token"]);
  });

  it("401 for an expired token, a foreign key and garbage", async () => {
    expect((await rejection(verify(await sign(admin, { exp: "-2m" })))).status).toBe(401);
    expect((await rejection(verify(await sign(admin, { key: otherKey })))).status).toBe(401);
    expect((await rejection(verify("a.b.c"))).status).toBe(401);
  });

  it("401 for alg=none", async () => {
    const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
    const now = Math.floor(Date.now() / 1000);
    const unsigned = `${enc({ alg: "none", typ: "JWT" })}.${enc({ ...admin, exp: now + 60 })}.`;
    expect((await rejection(verify(unsigned))).status).toBe(401);
  });

  it("refuses HS* and none in ADMIN_JWT_ALGORITHMS", () => {
    expect(() => loadConfig({ ADMIN_JWT_ALGORITHMS: "HS256" })).toThrow(/asymmetric/);
  });
});

describe("helpers", () => {
  it("rolesFrom reads dotted paths, arrays and space-separated strings", () => {
    expect(rolesFrom({ realm_access: { roles: ["a", 1, "b"] } }, "realm_access.roles")).toEqual([
      "a",
      "b",
    ]);
    expect(rolesFrom({ scp: "x y" }, "scp")).toEqual(["x", "y"]);
    expect(rolesFrom({}, "roles")).toEqual([]);
  });
  it("bearerToken requires the Bearer scheme", () => {
    expect(bearerToken("Bearer abc.def.ghi")).toBe("abc.def.ghi");
    expect(() => bearerToken(undefined)).toThrow(AuthError);
    expect(() => bearerToken("Basic xyz")).toThrow(AuthError);
  });
});
