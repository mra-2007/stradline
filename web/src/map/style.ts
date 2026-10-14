/* The map's surround: night ground, a desaturated OpenStreetMap basemap tinted
   to the slate palette, hillshade and terrain off the run's own Copernicus DEM,
   and one atmospheric sky.

   Sources credited on screen (Rule 1): "© OpenStreetMap contributors" for the
   basemap, "Copernicus DEM" for the terrain and the hillshade. */

import type { StyleSpecification } from "maplibre-gl";

export const C = {
  night: "#1B2430",
  panel: "#222C3A",
  ridge: "#33414F",
  snow: "#E9EEF2",
  muted: "#93A1AF",
  meltwater: "#6CC6C2",
  silt: "#B9A887",
  rescue: "#F2661B",
  saffron: "#F5B53D",
  lichen: "#8FB98B",
} as const;

export const OSM_ATTRIBUTION = "© OpenStreetMap contributors";
export const DEM_ATTRIBUTION = "Copernicus DEM";

export const TERRAIN_SOURCE = "copernicus-dem";
/** The same tiles again, as their own source: MapLibre renders hillshade
 *  better when it does not share a source with the 3D terrain. */
export const HILLSHADE_SOURCE = "copernicus-dem-shade";
export const OSM_SOURCE = "osm";

/** Flood classes, as the pipeline writes them into flood_polygons.geojson. */
export const CLASS = {
  floodHigh: 2,
  floodMedium: 3,
  debris: 4,
} as const;

export function baseStyle(terrainTiles: string | null): StyleSpecification {
  const sources: StyleSpecification["sources"] = {
    [OSM_SOURCE]: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 18,
      attribution: OSM_ATTRIBUTION,
    },
  };

  if (terrainTiles) {
    const dem = {
      type: "raster-dem" as const,
      tiles: [terrainTiles],
      tileSize: 256,
      encoding: "terrarium" as const,
      minzoom: 7,
      maxzoom: 14,
      attribution: DEM_ATTRIBUTION,
    };
    sources[TERRAIN_SOURCE] = dem;
    sources[HILLSHADE_SOURCE] = { ...dem };
  }

  const layers: StyleSpecification["layers"] = [
    {
      id: "ground",
      type: "background",
      paint: { "background-color": C.night },
    },
    {
      // The basemap is drained of colour and dimmed until it reads as rock in
      // the dark. The slate ground beneath does the tinting.
      id: "basemap",
      type: "raster",
      source: OSM_SOURCE,
      paint: {
        // Drained of colour and pulled right down, so what is left is the
        // grain of roads and rivers over the slate ground, not a day map.
        "raster-opacity": 0.3,
        "raster-saturation": -1,
        "raster-brightness-min": 0.02,
        "raster-brightness-max": 0.34,
        "raster-contrast": -0.18,
        "raster-fade-duration": 240,
      },
    },
  ];

  if (terrainTiles) {
    layers.push({
      id: "hillshade",
      type: "hillshade",
      source: HILLSHADE_SOURCE,
      paint: {
        "hillshade-exaggeration": 0.62,
        "hillshade-shadow-color": "#101922",
        "hillshade-highlight-color": "#495C6E",
        "hillshade-accent-color": "#1B2430",
        "hillshade-illumination-direction": 315,
      },
    });
  }

  return {
    version: 8,
    // No glyph server is declared: every label on this map is a DOM element,
    // so the style needs no fonts and the dashboard comes up with no network
    // beyond the basemap tiles.
    sky: {
      "sky-color": "#121A24",
      "horizon-color": "#2B3B4A",
      "fog-color": "#1B2430",
      "fog-ground-blend": 0.72,
      "horizon-fog-blend": 0.45,
      "sky-horizon-blend": 0.72,
      "atmosphere-blend": 0.86,
    },
    light: { anchor: "map", color: "#C9D6DF", intensity: 0.28, position: [1.5, 200, 42] },
    sources,
    layers,
  };
}
