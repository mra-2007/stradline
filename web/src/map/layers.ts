/* Every data layer on the map, and the few expressions that animate them.

   Layer spec: web/DESIGN.md section 5. Colours come from style.ts, which is
   the same palette as the CSS tokens. Each layer's source is one file the API
   served; a layer whose file the run never produced simply stays empty, and
   the legend row says so. */

import type { ExpressionSpecification, Map as MLMap } from "maplibre-gl";

import type { FeatureCollection } from "../lib/api";
import { C, CLASS } from "./style";

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

export const SRC = {
  flood: "sl-flood",
  lowConf: "sl-low-confidence",
  buildings: "sl-buildings",
  roads: "sl-roads",
  bridges: "sl-bridges",
  settlements: "sl-settlements",
  routeFocus: "sl-route-focus",
  highlight: "sl-highlight",
} as const;

export const LYR = {
  floodMedium: "flood-medium",
  floodHigh: "flood-high",
  debris: "debris",
  lowConf: "low-confidence",
  strandlineGlow: "strandline-glow",
  strandline: "strandline",
  roads: "cut-roads",
  buildings: "damaged-buildings",
  bridges: "bridges-out",
  settlements: "settlements",
  settlementRing: "settlement-ring",
  routeBefore: "route-before",
  routeAfter: "route-after",
  highlightFill: "highlight-fill",
  highlightLine: "highlight-line",
  highlightPoint: "highlight-point",
} as const;

/** The three groups the three sentences switch. */
export const GROUPS = {
  reached: [LYR.floodMedium, LYR.floodHigh, LYR.debris, LYR.lowConf, LYR.strandlineGlow, LYR.strandline],
  broke: [LYR.roads, LYR.buildings, LYR.bridges],
  stranded: [LYR.settlements, LYR.settlementRing],
} as const;

/* --- the reveal ----------------------------------------------------------- */

/** Opacity that sweeps downstream: a feature at `t` appears as `reveal`
 *  passes it, with a soft leading edge. `reveal` runs -0.12 .. 1.12. */
export function revealOpacity(base: number, reveal: number, edge = 0.12): ExpressionSpecification {
  return [
    "*",
    base,
    ["max", 0, ["min", 1, ["/", ["-", reveal, ["coalesce", ["get", "t"], 0]], edge]]],
  ] as ExpressionSpecification;
}

export const FULL_REVEAL = 1.2;

/* --- patterns the style needs before its layers ---------------------------- */

/** A fine diagonal hatch: the one texture on the map, for "radar cannot see
 *  well here". Drawn rather than loaded so the app needs no sprite. */
function hatchImage(size = 10): ImageData | null {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.clearRect(0, 0, size, size);
  ctx.strokeStyle = C.muted;
  ctx.lineWidth = 1.1;
  ctx.beginPath();
  ctx.moveTo(-1, size + 1);
  ctx.lineTo(size + 1, -1);
  ctx.moveTo(-1, 1);
  ctx.lineTo(1, -1);
  ctx.moveTo(size - 1, size + 1);
  ctx.lineTo(size + 1, size - 1);
  ctx.stroke();
  return ctx.getImageData(0, 0, size, size);
}

/** A ring with a slash through it: a bridge that is likely out. */
function bridgeOutImage(size = 22): ImageData | null {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const r = size / 2 - 3;
  ctx.clearRect(0, 0, size, size);
  ctx.fillStyle = "rgba(27,36,48,0.78)";
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = C.saffron;
  ctx.lineWidth = 1.8;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, r, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(size / 2 - r * 0.62, size / 2 + r * 0.62);
  ctx.lineTo(size / 2 + r * 0.62, size / 2 - r * 0.62);
  ctx.stroke();
  return ctx.getImageData(0, 0, size, size);
}

export function ensureImages(map: MLMap): void {
  if (!map.hasImage("sl-hatch")) {
    const hatch = hatchImage();
    if (hatch) map.addImage("sl-hatch", hatch, { pixelRatio: 2 });
  }
  if (!map.hasImage("sl-bridge-out")) {
    const bridge = bridgeOutImage();
    if (bridge) map.addImage("sl-bridge-out", bridge, { pixelRatio: 2 });
  }
}

/* --- installing the layers ------------------------------------------------ */

function addGeoJSONSource(map: MLMap, id: string, lineMetrics = false): void {
  if (map.getSource(id)) return;
  map.addSource(id, { type: "geojson", data: EMPTY as never, lineMetrics });
}

export function installDataLayers(map: MLMap): void {
  ensureImages(map);
  Object.values(SRC).forEach((id) =>
    addGeoJSONSource(map, id, id === SRC.routeFocus),
  );

  // Order matters: water under terrain features, action colours on top.
  map.addLayer({
    id: LYR.lowConf,
    type: "fill",
    source: SRC.lowConf,
    paint: {
      "fill-pattern": "sl-hatch",
      "fill-opacity": 0.3,
    },
  });

  map.addLayer({
    id: LYR.debris,
    type: "fill",
    source: SRC.flood,
    filter: ["==", ["get", "class_code"], CLASS.debris],
    paint: {
      "fill-color": C.silt,
      "fill-opacity": revealOpacity(0.5, FULL_REVEAL),
      "fill-outline-color": C.silt,
    },
  });

  map.addLayer({
    id: LYR.floodMedium,
    type: "fill",
    source: SRC.flood,
    filter: ["==", ["get", "class_code"], CLASS.floodMedium],
    paint: {
      "fill-color": C.meltwater,
      "fill-opacity": revealOpacity(0.45, FULL_REVEAL),
    },
  });

  map.addLayer({
    id: LYR.floodHigh,
    type: "fill",
    source: SRC.flood,
    filter: ["==", ["get", "class_code"], CLASS.floodHigh],
    paint: {
      "fill-color": C.meltwater,
      "fill-opacity": revealOpacity(0.92, FULL_REVEAL),
    },
  });

  // The strandline: the high-water line the product is named after. A blurred
  // pass for the glow, a crisp pass on top. Debris is not flood extent, so it
  // is not part of this line.
  const floodExtentFilter: ExpressionSpecification = [
    "any",
    ["==", ["get", "class_code"], CLASS.floodHigh],
    ["==", ["get", "class_code"], CLASS.floodMedium],
  ];

  map.addLayer({
    id: LYR.strandlineGlow,
    type: "line",
    source: SRC.flood,
    filter: floodExtentFilter,
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": C.meltwater,
      "line-width": 6,
      "line-blur": 6,
      "line-opacity": revealOpacity(0.35, FULL_REVEAL),
    },
  });

  map.addLayer({
    id: LYR.strandline,
    type: "line",
    source: SRC.flood,
    filter: floodExtentFilter,
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": "#A8E6E3",
      "line-width": 1.4,
      "line-opacity": revealOpacity(1, FULL_REVEAL),
    },
  });

  map.addLayer({
    id: LYR.roads,
    type: "line",
    source: SRC.roads,
    layout: { "line-cap": "butt" },
    paint: {
      "line-color": C.saffron,
      "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.6, 14, 3.2],
      "line-dasharray": [2, 1.6],
      "line-opacity": 0.95,
    },
  });

  map.addLayer({
    id: LYR.buildings,
    type: "fill-extrusion",
    source: SRC.buildings,
    paint: {
      "fill-extrusion-color": C.rescue,
      "fill-extrusion-height": 14,
      "fill-extrusion-base": 0,
      "fill-extrusion-opacity": 0.92,
    },
  });

  map.addLayer({
    id: LYR.bridges,
    type: "symbol",
    source: SRC.bridges,
    layout: {
      "icon-image": "sl-bridge-out",
      "icon-size": ["interpolate", ["linear"], ["zoom"], 9, 0.55, 14, 0.9],
      "icon-allow-overlap": true,
      "symbol-placement": "line-center",
    },
  });

  map.addLayer({
    id: LYR.settlements,
    type: "circle",
    source: SRC.settlements,
    paint: {
      // One zoom expression, outermost: a settlement grows with the map, and
      // within each zoom it grows with its cut-off probability.
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        9,
        ["interpolate", ["linear"], ["coalesce", ["get", "cut_off_probability"], 0], 0, 1.5, 1, 5],
        14,
        ["interpolate", ["linear"], ["coalesce", ["get", "cut_off_probability"], 0], 0, 2.4, 1, 8],
      ],
      "circle-color": [
        "interpolate",
        ["linear"],
        ["coalesce", ["get", "cut_off_probability"], 0],
        0,
        C.snow,
        0.5,
        C.saffron,
        1,
        C.rescue,
      ],
      // Thousands of settlements are mapped and none is hidden, but the ones
      // the flood did nothing to must not shout over the ones it cut off.
      "circle-opacity": [
        "step",
        ["coalesce", ["get", "cut_off_probability"], 0],
        0.3,
        0.05,
        0.6,
        0.5,
        0.95,
      ],
      "circle-stroke-color": "#101922",
      "circle-stroke-width": 0.6,
    },
  });

  // The selected settlement, ringed in snow. Filter is set on selection.
  map.addLayer({
    id: LYR.settlementRing,
    type: "circle",
    source: SRC.settlements,
    filter: ["==", ["get", "name"], " none"],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 7, 14, 11],
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": C.snow,
      "circle-stroke-width": 1.4,
    },
  });

  map.addLayer({
    id: LYR.routeBefore,
    type: "line",
    source: SRC.routeFocus,
    filter: ["==", ["get", "phase"], "before"],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": C.muted,
      "line-width": 1.8,
      "line-dasharray": [2, 2],
      "line-opacity": 0.8,
    },
  });

  map.addLayer({
    id: LYR.routeAfter,
    type: "line",
    source: SRC.routeFocus,
    filter: ["==", ["get", "phase"], "after"],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: {
      "line-color": C.saffron,
      "line-width": 2.6,
      "line-gradient": [
        "interpolate",
        ["linear"],
        ["line-progress"],
        0,
        C.saffron,
        1,
        C.saffron,
      ],
    },
  });

  map.addLayer({
    id: LYR.highlightFill,
    type: "fill",
    source: SRC.highlight,
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "fill-color": C.meltwater, "fill-opacity": 0.28 },
  });

  map.addLayer({
    id: LYR.highlightLine,
    type: "line",
    source: SRC.highlight,
    filter: ["!=", ["geometry-type"], "Point"],
    layout: { "line-join": "round", "line-cap": "round" },
    paint: { "line-color": "#A8E6E3", "line-width": 2.6, "line-opacity": 0.95 },
  });

  map.addLayer({
    id: LYR.highlightPoint,
    type: "circle",
    source: SRC.highlight,
    filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 6, 14, 10],
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": "#A8E6E3",
      "circle-stroke-width": 2,
    },
  });
}

/* --- updating -------------------------------------------------------------- */

export function setSourceData(map: MLMap, id: string, data: FeatureCollection | null): void {
  const source = map.getSource(id);
  if (source && "setData" in source) {
    (source as { setData: (d: unknown) => void }).setData(data ?? EMPTY);
  }
}

export function setReveal(map: MLMap, reveal: number): void {
  if (!map.getLayer(LYR.floodHigh)) return;
  map.setPaintProperty(LYR.floodHigh, "fill-opacity", revealOpacity(0.92, reveal));
  map.setPaintProperty(LYR.floodMedium, "fill-opacity", revealOpacity(0.45, reveal));
  map.setPaintProperty(LYR.debris, "fill-opacity", revealOpacity(0.5, reveal));
}

/** The strandline draws itself head to tail, a little behind the water. */
export function setStrandlineReveal(map: MLMap, reveal: number): void {
  if (!map.getLayer(LYR.strandline)) return;
  map.setPaintProperty(LYR.strandline, "line-opacity", revealOpacity(1, reveal, 0.08));
  map.setPaintProperty(LYR.strandlineGlow, "line-opacity", revealOpacity(0.35, reveal, 0.08));
}

export function setGroupVisible(map: MLMap, ids: readonly string[], visible: boolean): void {
  for (const id of ids) {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    }
  }
}

export function setLayerVisible(map: MLMap, id: string, visible: boolean): void {
  if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}

export function setSelectedSettlement(map: MLMap, name: string | null): void {
  if (!map.getLayer(LYR.settlementRing)) return;
  map.setFilter(LYR.settlementRing, ["==", ["get", "name"], name ?? " none"]);
}

/** Animate the detour drawing itself, as a gradient wipe along the line. */
export function setRouteProgress(map: MLMap, progress: number): void {
  if (!map.getLayer(LYR.routeAfter)) return;
  const p = Math.max(0.0001, Math.min(0.9999, progress));
  map.setPaintProperty(LYR.routeAfter, "line-gradient", [
    "interpolate",
    ["linear"],
    ["line-progress"],
    0,
    C.saffron,
    p,
    C.saffron,
    Math.min(p + 0.0001, 1),
    "rgba(245,181,61,0)",
    1,
    "rgba(245,181,61,0)",
  ]);
}
