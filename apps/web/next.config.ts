import type { NextConfig } from "next";

const config: NextConfig = {
  output: "export",
  // The disposable E2E server must not generate repository instruction files.
  agentRules: process.env.APP_ENV === "e2e" ? false : undefined,
  transpilePackages: [],
  images: { unoptimized: true }
};

export default config;
