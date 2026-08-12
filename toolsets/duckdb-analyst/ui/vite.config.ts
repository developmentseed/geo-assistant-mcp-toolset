import { resolve } from "node:path";
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// Build one self-contained view per pass (VIEW=<id>), inlined into a single
// HTML file in the Python package's views/ dir, where the runtime serves it as
// ui://<toolset>/<id>. Add a view: a new <id>.html + src/<id>.ts, a VIEWS
// entry in tools.py, and a build pass in package.json's "build" script.
const view = process.env.VIEW;
if (!view) {
  throw new Error("set VIEW=<view-id> (e.g. VIEW=table vite build)");
}

export default defineConfig({
  plugins: [viteSingleFile()],
  build: {
    outDir: resolve(__dirname, "../src/duckdb_analyst/views"),
    emptyOutDir: false,
    // vega alone is ~1.5MB minified; the bundle is a build-time resource, so
    // size is a download cost, not a page-weight one — silence the warning.
    chunkSizeWarningLimit: 3000,
    rollupOptions: {
      input: { [view]: resolve(__dirname, `${view}.html`) },
    },
  },
});
