/* Where the valley runs, from the run's own flood extent.

   The fly-in needs two things no file gives us: a bearing to look along, and,
   for every flood polygon, how far down the valley it sits so the water can
   fill downstream instead of fading in everywhere at once.

   Both come out of the geometry itself — the principal axis of the flood
   polygons' centroids — and the downstream direction comes from the DEM
   through MapLibre's own terrain sampler. Nothing here is keyed to an area, a
   date or an event (Rule 3), and nothing here produces a number the UI
   reports: it only drives the camera and the reveal.
*/

import type { FeatureCollection, GeoJSONFeature } from "../lib/api";

export interface ValleyAxis {
  /** Centre of the flood extent. */
  centre: [number, number];
  /** Unit vector pointing downstream, in locally-scaled lon/lat. */
  direction: [number, number];
  /** Camera bearing that looks downstream, degrees clockwise from north. */
  bearing: number;
  /** True when the downstream direction was settled by sampling the DEM. */
  orientedByTerrain: boolean;
}

function ringCentroid(coords: any): [number, number] | null {
  if (!Array.isArray(coords) || coords.length === 0) return null;
  let sx = 0;
  let sy = 0;
  let n = 0;
  for (const point of coords) {
    if (!Array.isArray(point) || typeof point[0] !== "number") continue;
    sx += point[0];
    sy += point[1];
    n += 1;
  }
  return n > 0 ? [sx / n, sy / n] : null;
}

/** A representative point for any geometry, cheap and good enough for an axis. */
export function featureCentroid(feature: GeoJSONFeature): [number, number] | null {
  const geometry = feature?.geometry;
  if (!geometry) return null;
  switch (geometry.type) {
    case "Point":
      return [geometry.coordinates[0], geometry.coordinates[1]];
    case "LineString":
      return ringCentroid(geometry.coordinates);
    case "Polygon":
      return ringCentroid(geometry.coordinates[0]);
    case "MultiPolygon":
      return ringCentroid(geometry.coordinates[0]?.[0]);
    case "MultiLineString":
      return ringCentroid(geometry.coordinates[0]);
    default:
      return null;
  }
}

export function collectionCentre(collection: FeatureCollection | null): [number, number] | null {
  const points = (collection?.features ?? [])
    .map(featureCentroid)
    .filter((p): p is [number, number] => p !== null);
  if (points.length === 0) return null;
  const sx = points.reduce((acc, p) => acc + p[0], 0);
  const sy = points.reduce((acc, p) => acc + p[1], 0);
  return [sx / points.length, sy / points.length];
}

/**
 * The principal axis of a set of flood polygons.
 *
 * @param sampleElevation optional DEM sampler; when it answers, the axis is
 *        turned to point downhill, which is downstream.
 */
export function valleyAxis(
  collection: FeatureCollection | null,
  sampleElevation?: (lng: number, lat: number) => number | null,
): ValleyAxis | null {
  const points = (collection?.features ?? [])
    .map(featureCentroid)
    .filter((p): p is [number, number] => p !== null);
  if (points.length < 2) return null;

  const cx = points.reduce((a, p) => a + p[0], 0) / points.length;
  const cy = points.reduce((a, p) => a + p[1], 0) / points.length;
  // Scale longitude so that one unit is one unit of distance at this latitude,
  // otherwise the axis of a north-south valley tilts with latitude.
  const kx = Math.cos((cy * Math.PI) / 180) || 1;

  let sxx = 0;
  let syy = 0;
  let sxy = 0;
  for (const [lng, lat] of points) {
    const dx = (lng - cx) * kx;
    const dy = lat - cy;
    sxx += dx * dx;
    syy += dy * dy;
    sxy += dx * dy;
  }

  // Leading eigenvector of the 2x2 covariance matrix, closed form.
  const theta = 0.5 * Math.atan2(2 * sxy, sxx - syy);
  let ux = Math.cos(theta);
  let uy = Math.sin(theta);

  let orientedByTerrain = false;
  if (sampleElevation) {
    const span = Math.max(
      ...points.map(([lng, lat]) => Math.abs((lng - cx) * kx * ux + (lat - cy) * uy)),
    );
    const head: [number, number] = [cx - (ux * span) / kx, cy - uy * span];
    const tail: [number, number] = [cx + (ux * span) / kx, cy + uy * span];
    const headZ = sampleElevation(head[0], head[1]);
    const tailZ = sampleElevation(tail[0], tail[1]);
    if (headZ !== null && tailZ !== null && Number.isFinite(headZ) && Number.isFinite(tailZ)) {
      orientedByTerrain = true;
      // Downstream is downhill: the axis must point from the high end to the low end.
      if (tailZ > headZ) {
        ux = -ux;
        uy = -uy;
      }
    }
  }

  // Bearing of (ux, uy) in map terms: y is north, x is east.
  const bearing = (Math.atan2(ux, uy) * 180) / Math.PI;

  return {
    centre: [cx, cy],
    direction: [ux, uy],
    bearing: ((bearing % 360) + 360) % 360,
    orientedByTerrain,
  };
}

/**
 * Tag every feature with `t`: 0 at the upstream end of the axis, 1 at the
 * downstream end. The fill reveal and the strandline wipe both key off it.
 * Returns a new collection; the input is untouched.
 */
export function withAlongValley(
  collection: FeatureCollection,
  axis: ValleyAxis | null,
): FeatureCollection {
  if (!axis) {
    return {
      ...collection,
      features: collection.features.map((f, i) => ({
        ...f,
        id: i + 1,
        properties: { ...f.properties, t: 0 },
      })),
    };
  }

  const [cx, cy] = axis.centre;
  const [ux, uy] = axis.direction;
  const kx = Math.cos((cy * Math.PI) / 180) || 1;

  const projected = collection.features.map((feature) => {
    const centre = featureCentroid(feature);
    if (!centre) return 0;
    return (centre[0] - cx) * kx * ux + (centre[1] - cy) * uy;
  });

  const lo = Math.min(...projected);
  const hi = Math.max(...projected);
  const span = hi - lo || 1;

  return {
    ...collection,
    features: collection.features.map((feature, index) => ({
      ...feature,
      id: index + 1,
      properties: {
        ...feature.properties,
        t: Number(((projected[index] - lo) / span).toFixed(4)),
      },
    })),
  };
}

/** Bounds of a set of features, as [[w, s], [e, n]]. */
export function featureBounds(
  features: GeoJSONFeature[],
): [[number, number], [number, number]] | null {
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;

  const visit = (coords: any) => {
    if (typeof coords?.[0] === "number" && typeof coords?.[1] === "number") {
      w = Math.min(w, coords[0]);
      e = Math.max(e, coords[0]);
      s = Math.min(s, coords[1]);
      n = Math.max(n, coords[1]);
      return;
    }
    if (Array.isArray(coords)) coords.forEach(visit);
  };

  for (const feature of features) visit(feature?.geometry?.coordinates);
  if (!Number.isFinite(w) || !Number.isFinite(s)) return null;
  return [
    [w, s],
    [e, n],
  ];
}
