import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

// Build one self-contained view per pass (VIEW=<id>), inlined into a single
// HTML file in the Python package's views/ dir, where the runtime serves it as
// ui://<toolset>/<id>. Add a view: a new <id>.html + src/<id>.tsx, a VIEWS
// entry in tools.py, and a build pass in package.json's "build" script.
const view = process.env.VIEW;
if (!view) {
  throw new Error("set VIEW=<view-id> (e.g. VIEW=panel vite build)");
}

export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: {
    outDir: resolve(__dirname, "../src/naip_imagery/views"),
    emptyOutDir: false,
    rollupOptions: {
      input: { [view]: resolve(__dirname, `${view}.html`) },
    },
  },
});
