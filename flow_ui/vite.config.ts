import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// One self-contained page: scripts and styles inlined into dist/index.html,
// which agent_app.py serves at /flow.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
});
