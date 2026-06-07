import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const envDir = "../..";
  const env = loadEnv(mode, envDir, "");
  const host = env.VITE_DEV_HOST || "127.0.0.1";
  const port = Number.parseInt(env.VITE_DEV_PORT || "5173", 10);
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    envDir,
    server: {
      host,
      port: Number.isFinite(port) ? port : 5173,
      proxy: {
        "/api": apiProxyTarget
      }
    }
  };
});
