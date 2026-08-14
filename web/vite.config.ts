import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `uvicorn mcp_agent_api.app:app --port 8765` (run from the repo root) serves
// the agent on 8765. Proxying to it means the browser only ever talks to one
// origin, so this client needs no CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
