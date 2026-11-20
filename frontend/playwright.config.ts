import { defineConfig } from "@playwright/test";

// Runs against an already running server (API + worker + built frontend):
//   signalserver-gui serve --with-worker
export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  use: {
    baseURL: process.env.SSG_E2E_URL ?? "http://127.0.0.1:8080",
    viewport: { width: 1440, height: 900 },
    launchOptions: {
      // Optional: use a system Chromium instead of Playwright's download.
      executablePath: process.env.PW_CHROMIUM_PATH || undefined,
      args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
    },
    trace: "off",
  },
});
