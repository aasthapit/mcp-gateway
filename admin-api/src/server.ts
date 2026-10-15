/** Process entry point: `npm run dev` (tsx watch) or `node dist/server.js`. */
import { buildApp } from "./app.js";
import { createVerifier } from "./auth.js";
import { loadConfig } from "./config.js";
import { connectMongo, ensureIndexes } from "./db.js";
import { createGatewayClient } from "./gateway.js";
import { buildValidators } from "./schemas.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const validators = buildValidators(config.schemasDir);
  const mongo = await connectMongo(config.mongoUri, config.mongoDb);
  const app = await buildApp({
    config,
    mongo,
    validators,
    verifier: createVerifier(config),
    gateway: createGatewayClient(config.gatewayUrl, config.gatewayToken, config.gatewayTimeoutMs),
  });
  await ensureIndexes(mongo.db).catch((err: unknown) =>
    app.log.warn({ err }, "could not ensure history indexes"),
  );
  if (!config.gatewayToken)
    app.log.warn(
      "ADMIN_GATEWAY_INTERNAL_TOKEN not set: /api/catalog, /api/preview, health and reload answer 503",
    );

  let closing = false;
  const shutdown = async (signal: string) => {
    if (closing) return;
    closing = true;
    app.log.info({ signal }, "shutting down");
    await app.close();
    await mongo.client.close();
    process.exit(0);
  };
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("SIGINT", () => void shutdown("SIGINT"));

  await app.listen({ host: config.host, port: config.port });
  app.log.info(
    {
      issuer: config.oidcIssuer,
      audience: config.jwtAudience,
      role: config.requiredRole,
      gateway: config.gatewayUrl,
      db: config.mongoDb,
    },
    "admin-api ready",
  );
}

main().catch((err: unknown) => {
  console.error(err);
  process.exit(1);
});
