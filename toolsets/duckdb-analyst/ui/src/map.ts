// Map view shared by the geo tools: one Leaflet map over whichever GeoJSON
// the result carries — `place` (get_place, one Feature), `search_area`
// (get_search_area, a FeatureCollection), or `places` (places_within_area).
// Vectors are self-contained; the basemap tiles are the one network fetch, so
// on a host whose sandbox blocks them the features still render on a plain
// surface. Popups show URLs as text: the iframe runs sandbox="allow-scripts"
// only, so links could not open anyway.
import { onData } from "@developmentseed/mcp-view";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { Feature, FeatureCollection } from "geojson";

import "./styles.css";
import { dark, tokens } from "./theme";

// The union of the three geo ToolResults in geo_tools.py.
interface MapResult {
  message?: string;
  place?: Feature;
  search_area?: FeatureCollection;
  places?: FeatureCollection;
}

// Carto's free basemaps, matched to the color scheme; attribution per their
// terms plus OSM's.
const TILE_URL = dark
  ? "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
  : "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
const ATTRIBUTION = "&copy; OpenStreetMap contributors &copy; CARTO";

const root = document.getElementById("root")!;

function featuresOf(data: MapResult): Feature[] {
  if (data.place) return [data.place];
  const collection = data.search_area ?? data.places;
  return collection?.features ?? [];
}

function popupContent(feature: Feature): HTMLElement | null {
  const properties = feature.properties ?? {};
  const box = document.createElement("div");
  const name = properties["name"];
  if (typeof name === "string" && name) {
    const title = document.createElement("div");
    title.className = "popup-title";
    title.textContent = name;
    box.append(title);
  }
  for (const key of ["category", "buffer_km"]) {
    const value = properties[key];
    if (value == null) continue;
    const line = document.createElement("div");
    line.className = "popup-detail";
    line.textContent = key === "buffer_km" ? `${value} km buffer` : String(value);
    box.append(line);
  }
  const websites = properties["websites"];
  const website = Array.isArray(websites) ? websites[0] : undefined;
  if (typeof website === "string" && website) {
    const line = document.createElement("div");
    line.className = "popup-detail";
    line.textContent = website;
    box.append(line);
  }
  return box.childElementCount ? box : null;
}

function render(data: MapResult): void {
  root.textContent = "";
  const view = document.createElement("div");
  view.className = "view";
  root.append(view);

  const features = featuresOf(data);
  if (!features.length) {
    const note = document.createElement("p");
    note.className = "view-note";
    note.textContent = data.message ?? "Nothing to map.";
    view.append(note);
    return;
  }

  const host = document.createElement("div");
  host.className = "map-host";
  view.append(host);

  const map = L.map(host, { zoomControl: true });
  L.tileLayer(TILE_URL, { maxZoom: 19, attribution: ATTRIBUTION }).addTo(map);

  const collection: FeatureCollection = { type: "FeatureCollection", features };
  const layer = L.geoJSON(collection, {
      // Areas: 2px accent outline, low-opacity fill, per the mark specs.
      // A function, not an object: Leaflet applies `style` to point layers
      // too, and an object would override pointToLayer's marker styling.
      style: (feature) =>
        feature?.geometry.type === "Point" || feature?.geometry.type === "MultiPoint"
          ? {}
          : {
              color: tokens.accent,
              weight: 2,
              fillColor: tokens.accent,
              fillOpacity: 0.12,
            },
      // Points: ≥8px vector markers with a surface ring — no icon sprites, so
      // the bundle stays self-contained.
      pointToLayer: (_feature, latlng) =>
        L.circleMarker(latlng, {
          radius: 7,
          weight: 2,
          color: tokens.surface,
          fillColor: tokens.accent,
          fillOpacity: 0.95,
        }),
      onEachFeature: (feature, featureLayer) => {
        const content = popupContent(feature);
        if (content) featureLayer.bindPopup(content);
        const name = feature.properties?.["name"];
        if (typeof name === "string" && name) {
          featureLayer.bindTooltip(name);
        }
      },
    },
  ).addTo(map);

  map.fitBounds(layer.getBounds().pad(0.15), { maxZoom: 16 });
}

onData<MapResult>(render);
