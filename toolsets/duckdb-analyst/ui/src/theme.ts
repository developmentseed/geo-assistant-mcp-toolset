// The dataviz reference palette, mirrored from styles.css for the parts that
// render to canvas/SVG (Vega, Leaflet vectors) and so cannot read CSS custom
// properties. Both mode columns are validated palettes — swap values here and
// in styles.css together on a rebrand.

export const dark: boolean = window.matchMedia(
  "(prefers-color-scheme: dark)",
).matches;

interface Tokens {
  text: string;
  secondary: string;
  line: string;
  accent: string;
  surface: string;
  category: string[];
  ramp: string[];
}

const LIGHT: Tokens = {
  text: "#0b0b0b",
  secondary: "#52514e",
  line: "#ececea",
  accent: "#2a78d6",
  surface: "#fcfcfb",
  category: [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
  ],
  ramp: [
    "#cde2fb",
    "#9ec5f4",
    "#6da7ec",
    "#3987e5",
    "#256abf",
    "#184f95",
    "#0d366b",
  ],
};

const DARK: Tokens = {
  ...LIGHT,
  text: "#ffffff",
  secondary: "#c3c2b7",
  line: "#383835",
  accent: "#3987e5",
  surface: "#1a1a19",
  category: [
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
  ],
};

export const tokens: Tokens = dark ? DARK : LIGHT;

/** Vega-Lite config: theme defaults only — a spec's own choices still win. */
export function vegaConfig(): Record<string, unknown> {
  return {
    background: "transparent",
    font: "system-ui, sans-serif",
    range: {
      category: tokens.category,
      ramp: tokens.ramp,
      heatmap: tokens.ramp,
    },
    axis: {
      labelColor: tokens.secondary,
      titleColor: tokens.secondary,
      gridColor: tokens.line,
      domainColor: tokens.line,
      tickColor: tokens.line,
    },
    legend: { labelColor: tokens.secondary, titleColor: tokens.secondary },
    title: { color: tokens.text },
    view: { stroke: null },
    // The hover layer: every mark gets an encoding-driven tooltip by default.
    mark: { tooltip: true },
    bar: { cornerRadiusEnd: 4 },
    line: { strokeWidth: 2 },
    point: { size: 72, filled: true },
  };
}
