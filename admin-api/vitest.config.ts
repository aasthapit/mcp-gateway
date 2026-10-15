import { defineConfig } from "vitest/config";

// unit: no services needed. live: the local stack (`make up && make seed`); every live
// suite skips itself when Mongo / Keycloak are unreachable.
export default defineConfig({
  test: {
    projects: [
      { test: { name: "unit", include: ["test/unit/**/*.test.ts"], environment: "node" } },
      {
        test: {
          name: "live",
          include: ["test/live/**/*.test.ts"],
          environment: "node",
          testTimeout: 90_000,
          hookTimeout: 90_000,
          fileParallelism: false,
        },
      },
    ],
  },
});
