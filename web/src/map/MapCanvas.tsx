/* The map: full-bleed, 3D, and the owner of every MapLibre call.
   React tells it what run to show; it tells React what was clicked. */

import { useEffect, useRef } from "react";
import {
  Map as MapLibreMap,
  NavigationControl,
  ScaleControl,
  type LngLat,
  type Map as MLMap,
  type MapMouseEvent,
  setWorkerUrl,
} from "maplibre-gl";
// MapLibre v6 loads its worker as a separate module. Handing Vite the file
// with ?url makes it a hashed asset of this build, so the dashboard needs no
// CDN and works offline.
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";

setWorkerUrl(maplibreWorkerUrl);

import { api, type FeatureCollection, type GeoJSONFeature, type LayerName, type LoadedLayer } from "../lib/api";
import { startFlyIn, restingViewpoint, prefersReducedMotion, type Viewpoint } from "./flyIn";
import {
  FULL_REVEAL,
  GROUPS,
  LYR,
  SRC,
  installDataLayers,
  setGroupVisible,
  setLayerVisible,
  setReveal,
  setRouteProgress,
  setSelectedSettlement,
  setSourceData,
  setStrandlineReveal,
} from "./layers";
import { C, HILLSHADE_SOURCE, TERRAIN_SOURCE, baseStyle } from "./style";
import { featureBounds, valleyAxis, withAlongValley } from "./valley";

export interface MapHandle {
  flyToFeatures(features: GeoJSONFeature[]): void;
  showHighlight(features: GeoJSONFeature[]): void;
  clearHighlight(): void;
  drawRouteFocus(features: GeoJSONFeature[]): void;
  clearRouteFocus(): void;
  setDrawMode(on: boolean): void;
  resetView(): void;
  skipFlyIn(): void;
}

export type GroupKey = keyof typeof GROUPS;

interface Props {
  runId: string;
  bbox: [number, number, number, number] | null;
  layers: Partial<Record<LayerName, LoadedLayer>>;
  visibleGroups: Record<GroupKey, boolean>;
  showLowConfidence: boolean;
  selectedSettlement: string | null;
  drawMode: boolean;
  /** When set, the map starts here instead of flying in (handing over from the intro). */
  openFrom?: Viewpoint | null;
  onReady(handle: MapHandle): void;
  onSelectSettlement(properties: Record<string, any> | null): void;
  onBoxDrawn(bbox: [number, number, number, number]): void;
  onViewpoint(viewpoint: Viewpoint): void;
  onFlyInEnd(): void;
}

const DRAW_SRC = "sl-draw-box";
const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

export function MapCanvas(props: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MLMap | null>(null);
  const ready = useRef(false);
  const flownFor = useRef<string | null>(null);
  const restRef = useRef<Viewpoint | null>(null);
  const routeFrame = useRef(0);
  const latest = useRef(props);
  latest.current = props;

  /* --- create the map once ------------------------------------------------ */
  useEffect(() => {
    if (!container.current || mapRef.current) return;

    const map = new MapLibreMap({
      container: container.current,
      style: baseStyle(api.terrainTiles(latest.current.runId)),
      center: latest.current.bbox
        ? [
            (latest.current.bbox[0] + latest.current.bbox[2]) / 2,
            (latest.current.bbox[1] + latest.current.bbox[3]) / 2,
          ]
        : [85.27, 28.07],
      zoom: 8,
      pitch: 0,
      bearing: 0,
      attributionControl: false,
      maxPitch: 78,
      pixelRatio: Math.min(window.devicePixelRatio, 1.5),
      fadeDuration: 0,
      canvasContextAttributes: { antialias: false, preserveDrawingBuffer: true },
      dragRotate: true,
    });
    mapRef.current = map;

    map.addControl(new NavigationControl({ visualizePitch: true }), "bottom-right");
    map.addControl(new ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

    map.on("load", () => {
      ready.current = true;
      if (map.getSource(TERRAIN_SOURCE)) {
        // No vertical exaggeration: the valley is steep enough to read true.
        map.setTerrain({ source: TERRAIN_SOURCE, exaggeration: 1 });
      }
      installDataLayers(map);
      if (!map.getSource(DRAW_SRC)) {
        map.addSource(DRAW_SRC, { type: "geojson", data: EMPTY as never });
        map.addLayer({
          id: "draw-box-fill",
          type: "fill",
          source: DRAW_SRC,
          paint: { "fill-color": C.meltwater, "fill-opacity": 0.12 },
        });
        map.addLayer({
          id: "draw-box-line",
          type: "line",
          source: DRAW_SRC,
          paint: { "line-color": C.meltwater, "line-width": 1.4, "line-dasharray": [3, 2] },
        });
      }
      pushData();
      pushVisibility();

      map.on("click", LYR.settlements, (event) => {
        const feature = event.features?.[0];
        if (feature) latest.current.onSelectSettlement(feature.properties as Record<string, any>);
      });
      map.on("mouseenter", LYR.settlements, () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", LYR.settlements, () => {
        map.getCanvas().style.cursor = "";
      });
      map.on("moveend", () => {
        latest.current.onViewpoint({
          center: [map.getCenter().lng, map.getCenter().lat],
          zoom: map.getZoom(),
          bearing: map.getBearing(),
          pitch: map.getPitch(),
        });
      });

      latest.current.onReady(handle);
    });

    return () => {
      map.remove();
      mapRef.current = null;
      ready.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* --- data --------------------------------------------------------------- */

  const pushData = () => {
    const map = mapRef.current;
    if (!map || !ready.current) return;
    const { layers, bbox, runId } = latest.current;

    const flood = layers.flood?.data ?? null;
    const sampler = map.getTerrain()
      ? (lng: number, lat: number) => map.queryTerrainElevation([lng, lat])
      : undefined;
    const axis = valleyAxis(flood, sampler);
    const tagged = flood ? withAlongValley(flood, axis) : null;

    setSourceData(map, SRC.flood, tagged);
    setSourceData(map, SRC.lowConf, layers.low_confidence?.data ?? null);
    setSourceData(map, SRC.buildings, layers.damaged_buildings?.data ?? null);
    setSourceData(map, SRC.roads, layers.cut_roads?.data ?? null);
    setSourceData(map, SRC.bridges, layers.bridges_out?.data ?? null);
    setSourceData(map, SRC.settlements, layers.settlements?.data ?? null);

    if (!bbox) return;
    const floodBounds = tagged ? featureBounds(tagged.features) : null;
    const rest = restingViewpoint(map, bbox, axis?.bearing ?? 0, floodBounds);
    restRef.current = rest;

    if (flownFor.current === runId) return;
    flownFor.current = runId;

    const openFrom = latest.current.openFrom;
    if (openFrom) {
      // Handed over from the intro: we are already looking at the valley.
      map.jumpTo(openFrom);
      map.easeTo({ ...rest, duration: prefersReducedMotion() ? 0 : 1400 });
      setReveal(map, FULL_REVEAL);
      setStrandlineReveal(map, FULL_REVEAL);
      latest.current.onFlyInEnd();
      return;
    }

    const flight = startFlyIn(map, rest, latest.current.onFlyInEnd);
    skipRef.current = () => flight.finish();
  };

  const skipRef = useRef<() => void>(() => {});

  useEffect(() => {
    pushData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.layers, props.bbox, props.runId]);

  /* --- the terrain source follows the run --------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready.current) return;
    // A new run means a new DEM: swap the style's terrain tiles in place.
    for (const id of [TERRAIN_SOURCE, HILLSHADE_SOURCE]) {
      const source = map.getSource(id);
      if (source && "setTiles" in source) {
        (source as { setTiles: (t: string[]) => void }).setTiles([api.terrainTiles(props.runId)]);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.runId]);

  /* --- visibility --------------------------------------------------------- */

  const pushVisibility = () => {
    const map = mapRef.current;
    if (!map || !ready.current) return;
    const { visibleGroups, showLowConfidence } = latest.current;
    (Object.keys(GROUPS) as GroupKey[]).forEach((key) => {
      setGroupVisible(map, GROUPS[key], visibleGroups[key]);
    });
    setLayerVisible(map, LYR.lowConf, visibleGroups.reached && showLowConfidence);
  };

  useEffect(() => {
    pushVisibility();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.visibleGroups, props.showLowConfidence]);

  useEffect(() => {
    const map = mapRef.current;
    if (map && ready.current) setSelectedSettlement(map, props.selectedSettlement);
  }, [props.selectedSettlement]);

  /* --- draw a box --------------------------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready.current) return;
    const canvas = map.getCanvas();

    if (!props.drawMode) {
      setSourceData(map, DRAW_SRC, null);
      canvas.style.cursor = "";
      return;
    }

    canvas.style.cursor = "crosshair";
    map.dragPan.disable();
    map.dragRotate.disable();

    let start: LngLat | null = null;

    const rectangle = (a: LngLat, b: LngLat): FeatureCollection => ({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: {},
          geometry: {
            type: "Polygon",
            coordinates: [
              [
                [a.lng, a.lat],
                [b.lng, a.lat],
                [b.lng, b.lat],
                [a.lng, b.lat],
                [a.lng, a.lat],
              ],
            ],
          },
        },
      ],
    });

    let moveFrame = 0;
    let pendingMove: MapMouseEvent | null = null;

    const onDown = (event: MapMouseEvent) => {
      start = event.lngLat;
    };
    const onMove = (event: MapMouseEvent) => {
      if (!start) return;
      pendingMove = event;
      if (moveFrame) return;
      moveFrame = requestAnimationFrame(() => {
        moveFrame = 0;
        if (start && pendingMove) setSourceData(map, DRAW_SRC, rectangle(start, pendingMove.lngLat));
      });
    };
    const onUp = (event: MapMouseEvent) => {
      if (!start) return;
      if (moveFrame) {
        cancelAnimationFrame(moveFrame);
        moveFrame = 0;
      }
      const a = start;
      const b = event.lngLat;
      start = null;
      const bbox: [number, number, number, number] = [
        Math.min(a.lng, b.lng),
        Math.min(a.lat, b.lat),
        Math.max(a.lng, b.lng),
        Math.max(a.lat, b.lat),
      ];
      setSourceData(map, DRAW_SRC, rectangle(a, b));
      if (bbox[2] - bbox[0] > 0.004 && bbox[3] - bbox[1] > 0.004) {
        latest.current.onBoxDrawn(bbox);
      }
    };

    map.on("mousedown", onDown);
    map.on("mousemove", onMove);
    map.on("mouseup", onUp);

    return () => {
      if (moveFrame) cancelAnimationFrame(moveFrame);
      map.off("mousedown", onDown);
      map.off("mousemove", onMove);
      map.off("mouseup", onUp);
      map.dragPan.enable();
      map.dragRotate.enable();
      canvas.style.cursor = "";
    };
  }, [props.drawMode]);

  /* --- the handle React drives the camera with ---------------------------- */

  const handle: MapHandle = {
    flyToFeatures(features) {
      const map = mapRef.current;
      if (!map) return;
      const bounds = featureBounds(features);
      if (!bounds) return;
      const [[w, s], [e, n]] = bounds;
      const pad = Math.max(0.004, (e - w) * 0.25, (n - s) * 0.25);
      map.fitBounds(
        [
          [w - pad, s - pad],
          [e + pad, n + pad],
        ],
        {
          padding: { top: 60, bottom: 180, left: 260, right: 420 },
          pitch: Math.max(45, map.getPitch()),
          bearing: map.getBearing(),
          duration: prefersReducedMotion() ? 0 : 1200,
          maxZoom: 15,
        },
      );
    },

    showHighlight(features) {
      const map = mapRef.current;
      if (!map) return;
      setSourceData(map, SRC.highlight, { type: "FeatureCollection", features });
    },

    clearHighlight() {
      const map = mapRef.current;
      if (map) setSourceData(map, SRC.highlight, null);
    },

    drawRouteFocus(features) {
      const map = mapRef.current;
      if (!map) return;
      setSourceData(map, SRC.routeFocus, { type: "FeatureCollection", features });
      if (routeFrame.current) cancelAnimationFrame(routeFrame.current);
      if (prefersReducedMotion()) {
        setRouteProgress(map, 1);
        return;
      }
      const t0 = performance.now();
      const step = () => {
        const p = Math.min(1, (performance.now() - t0) / 700);
        setRouteProgress(map, p);
        if (p < 1) routeFrame.current = requestAnimationFrame(step);
      };
      routeFrame.current = requestAnimationFrame(step);
    },

    clearRouteFocus() {
      const map = mapRef.current;
      if (map) setSourceData(map, SRC.routeFocus, null);
    },

    setDrawMode() {
      /* driven by the drawMode prop; here so callers have one surface */
    },

    resetView() {
      const map = mapRef.current;
      if (map && restRef.current) {
        map.easeTo({ ...restRef.current, duration: prefersReducedMotion() ? 0 : 900 });
      }
    },

    skipFlyIn() {
      skipRef.current();
    },
  };

  return <div className="map" ref={container} aria-label="Flood map of the selected area" />;
}
