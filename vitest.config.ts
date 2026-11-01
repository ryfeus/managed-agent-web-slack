import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["apps/*/tests/**/*.test.ts", "apps/web/**/*.test.ts", "e2e/helpers/**/*.test.ts"]
  }
});
