// Chart view for `chart`: renders the tool's completed Vega-Lite spec. The
// theme config supplies defaults (palette, recessive axes, tooltips); any
// mark/encoding choice the spec makes itself wins over it.
import { onData } from "@developmentseed/mcp-view";
import embed, { type VisualizationSpec } from "vega-embed";

import "./styles.css";
import { vegaConfig } from "./theme";

// Mirror of ChartResult in tools.py — the tool's structuredContent.
interface ChartResult {
  message?: string;
  spec?: Record<string, unknown>;
}

const root = document.getElementById("root")!;

function note(text: string): void {
  const p = document.createElement("p");
  p.className = "view-note";
  p.textContent = text;
  root.append(p);
}

async function render(data: ChartResult): Promise<void> {
  root.textContent = "";
  if (!data.spec) {
    note(data.message ?? "No chart spec in the result.");
    return;
  }
  const host = document.createElement("div");
  host.className = "view chart-host";
  root.append(host);
  // Fill the panel when the spec doesn't size itself; its own width wins.
  const spec = { width: "container", ...data.spec } as VisualizationSpec;
  try {
    await embed(host, spec, {
      actions: false,
      config: vegaConfig(),
    });
  } catch (error) {
    host.remove();
    note(`Could not render the chart spec: ${String(error)}`);
  }
}

onData<ChartResult>((data) => void render(data));
