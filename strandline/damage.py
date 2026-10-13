"""What the flood broke: buildings, road segments and bridges, from the extent.

This module intersects the pipeline's OWN flood extent with the pre-event
OpenStreetMap snapshot (`strandline.osm`). It makes no detection of its own and
reads no reference map, so everything it reports inherits the flood map's
accuracy - which is why every output carries the flood extent it was computed
from, and why `eval/damage_eval.py` scores the overlay built from our extent
against the same overlay built from a reference extent.

WHAT COUNTS AS DAMAGE, AND WHY EACH RULE IS SHAPED THAT WAY

building damaged
    its footprint INTERSECTS the flood extent grown by `DamageParams.buffer_px`
    (1 px = 20 m). Intersection, not centroid-inside: a rescuer needs to know
    that water reached a building, and a 20 m cell that clips the corner of a
    footprint is water reaching it. The buffer is a geolocation tolerance set
    from imaging and OSM positional accuracy, never from a score - see
    DamageParams.

road segment cut
    roads are split at every junction with another road first, so "cut" names a
    stretch between two junctions rather than a whole OSM way that may run for
    kilometres. A segment is cut when a PART OF IT lies inside the buffered
    extent (intersection length > 0), not merely when it touches the boundary at
    a point. The flooded length of each cut segment is reported, because "200 m
    of this road is under water" and "this 8 km road is cut" are different
    operational facts and the second one alone is misleading.

bridge likely out, unverified
    a bridge way that crosses a waterway whose CHANNEL, within
    `DamageParams.bridge_reach_m` of the crossing, lies inside the extent. The
    reach matters: a river in flood ten kilometres downstream says nothing about
    this crossing. The label is `config.BRIDGE_UNVERIFIED_LABEL` and is carried
    on every feature and every count, because radar and a DEM cannot establish
    that a bridge is gone - only that the channel it crosses was flooded, which
    is a reason to check it.

DEBRIS IS NOT CONSUMED HERE

The extent classes come from `ExtentParams.flood_extent_classes()`. Since
2026-10-12 the shipped extent excludes the debris class, whose measured
precision against the only expert mass-movement reference in the dev set is
0.0, and that decision explicitly forbids the damage overlay from consuming it.
Passing the classes through ExtentParams rather than hardcoding a tuple is what
keeps that true here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from rasterio.features import shapes
from scipy import ndimage
from shapely.errors import GEOSException
from shapely.geometry import MultiPoint, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import split, unary_union

from strandline.config import (
    BRIDGE_UNVERIFIED_LABEL,
    DamageParams,
    ExtentParams,
)

EIGHT = np.ones((3, 3), dtype=bool)

DAMAGE_CAVEAT = (
    "Damage is inferred by intersecting this run's own flood extent with a pre-event "
    "OpenStreetMap snapshot. It inherits the flood map's accuracy and is not an inspection: "
    "a flagged building is one the mapped water reached, and a bridge is never confirmed "
    "destroyed. Educational prototype."
)


# --- the flood extent as a geometry -------------------------------------------

def extent_mask(classes: np.ndarray, extent: ExtentParams | None = None,
                buffer_px: int | None = None) -> np.ndarray:
    """The flood extent, grown by `buffer_px` pixels (8-connected dilation).

    `buffer_px=None` resolves from `DamageParams()`, so this signature is not a
    second home for the value (Rule 3.7): config.py declares it once.
    """
    ep = extent if extent is not None else ExtentParams()
    buffer_px = DamageParams().buffer_px if buffer_px is None else buffer_px
    mask = np.isin(classes, list(ep.flood_extent_classes()))
    if buffer_px > 0 and mask.any():
        mask = ndimage.binary_dilation(mask, EIGHT, iterations=buffer_px)
    return mask


def extent_geometry(classes: np.ndarray, grid, extent: ExtentParams | None = None,
                    buffer_px: int | None = None) -> BaseGeometry:
    """The buffered flood extent as one shapely geometry in the GRID's CRS.

    Projected metres, so every length this module reports is metres and no
    geographic-degree length is ever computed. An empty extent gives an empty
    geometry rather than None, so callers need no special case: nothing
    intersects it and every count comes out zero, which is the right answer.
    """
    mask = extent_mask(classes, extent, buffer_px)
    if not mask.any():
        return unary_union([])
    polys = [shape(geom) for geom, value in
             shapes(mask.astype(np.uint8), mask=mask, transform=grid.transform, connectivity=8)
             if int(value) == 1]
    return unary_union(polys)


# --- splitting a road network at its junctions --------------------------------

def _points_of(geom) -> list[Point]:
    """Every point of an intersection result, whatever its type.

    Two ways that merely cross give a Point; ways that share a node give a
    Point; ways digitised along the same alignment give a LineString, whose
    ENDPOINTS are the junctions - so those are taken rather than the overlap
    being discarded.
    """
    if geom.is_empty:
        return []
    kind = geom.geom_type
    if kind == "Point":
        return [geom]
    if kind == "MultiPoint":
        return list(geom.geoms)
    if kind in ("LineString", "LinearRing"):
        return [Point(geom.coords[0]), Point(geom.coords[-1])]
    if kind.startswith("Multi") or kind == "GeometryCollection":
        out: list[Point] = []
        for part in geom.geoms:
            out.extend(_points_of(part))
        return out
    return []


def split_at_junctions(gdf, min_segment_m: float | None = None):
    """Split every line where another line of the same set meets it.

    Why at all: an OSM way is an editing unit, not a stretch of road. One way
    can run 20 km through a valley, so flagging "the way" as cut tells a
    rescuer almost nothing and makes any count of cut roads depend on how
    mappers happened to divide the network. Splitting at junctions makes a
    segment the thing that can actually be routed around.

    Returns a GeoDataFrame with every original column plus `segment_id`
    (`<osm id>#<n>`) and `segment_length_m`. Fragments shorter than
    `min_segment_m` are dropped as node-snapping slivers (see DamageParams).
    """
    import geopandas as gpd
    from shapely import STRtree

    # None resolves from config, so this signature declares no value (Rule 3.7).
    min_segment_m = DamageParams().min_segment_m if min_segment_m is None else min_segment_m
    if len(gdf) == 0:
        out = gdf.copy()
        for col in ("segment_id", "segment_length_m"):
            if col not in out.columns:
                out[col] = []
        return out

    geoms = list(gdf.geometry)
    tree = STRtree(geoms)
    rows, geometries = [], []
    for i, line in enumerate(geoms):
        if line is None or line.is_empty or line.geom_type not in ("LineString",
                                                                   "MultiLineString"):
            continue
        cuts: list[Point] = []
        for j in tree.query(line):
            if int(j) == i:
                continue
            cuts.extend(_points_of(line.intersection(geoms[int(j)])))
        pieces = [line]
        if cuts:
            try:
                pieces = list(split(line, MultiPoint(cuts)).geoms)
            except (GEOSException, ValueError):
                # A degenerate splitter (duplicate or off-line points) must not
                # lose the road: keep it whole, which only ever makes a segment
                # longer and so the cut count more conservative. Narrow on
                # purpose - anything else here is a bug and should surface.
                pieces = [line]
        base = gdf.iloc[i].drop(labels="geometry", errors="ignore").to_dict()
        osm_id = base.get("@osmId") or f"row/{i}"
        kept = 0
        for piece in pieces:
            if piece.is_empty or piece.length < min_segment_m:
                continue
            rows.append({**base, "segment_id": f"{osm_id}#{kept}",
                         "segment_length_m": round(float(piece.length), 3)})
            geometries.append(piece)
            kept += 1
    return gpd.GeoDataFrame(rows, geometry=geometries, crs=gdf.crs)


# --- the three assessments ----------------------------------------------------

def damaged_buildings(buildings, extent_geom: BaseGeometry):
    """Footprints that intersect the buffered extent."""
    if len(buildings) == 0 or extent_geom.is_empty:
        return buildings.iloc[0:0]
    return buildings[buildings.intersects(extent_geom)]


def cut_road_segments(segments, extent_geom: BaseGeometry):
    """Segments with a PART inside the buffered extent, plus flooded length.

    Returns a copy with `flooded_length_m` and `flooded_fraction` added. A
    point-only touch of the extent boundary gives zero flooded length and is
    not a cut: the 20 m buffer already carries the geolocation tolerance, so a
    tangent is not evidence that water is on the road.
    """
    out = segments.iloc[0:0].copy()
    if len(segments) == 0 or extent_geom.is_empty:
        for col in ("flooded_length_m", "flooded_fraction"):
            if col not in out.columns:
                out[col] = []
        return out
    inside = segments.geometry.intersection(extent_geom)
    flooded = inside.length
    keep = flooded > 0
    out = segments[keep].copy()
    out["flooded_length_m"] = flooded[keep].round(3).to_numpy()
    total = out.geometry.length.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        out["flooded_fraction"] = np.where(total > 0,
                                           (out["flooded_length_m"] / total).round(4), 0.0)
    return out


def bridges_likely_out(bridges, waterways, extent_geom: BaseGeometry,
                       reach_m: float | None = None):
    """Bridge ways crossing a waterway whose nearby channel is in the extent.

    Returns a copy with `crossing_waterway`, `crossed_channel_flooded_m` and
    `status` (`config.BRIDGE_UNVERIFIED_LABEL`) added.
    """
    import geopandas as gpd
    from shapely import STRtree

    # None resolves from config, so this signature declares no value (Rule 3.7).
    reach_m = DamageParams().bridge_reach_m if reach_m is None else reach_m
    empty = bridges.iloc[0:0].copy()
    for col in ("crossing_waterway", "crossed_channel_flooded_m", "status"):
        if col not in empty.columns:
            empty[col] = []
    if len(bridges) == 0 or len(waterways) == 0 or extent_geom.is_empty:
        return empty

    water_geoms = list(waterways.geometry)
    tree = STRtree(water_geoms)
    rows, geometries = [], []
    for i, bridge in enumerate(bridges.geometry):
        if bridge is None or bridge.is_empty:
            continue
        best_len, best_name = 0.0, None
        for j in tree.query(bridge):
            channel = water_geoms[int(j)]
            crossing = bridge.intersection(channel)
            points = _points_of(crossing)
            if not points:
                continue
            # Only the channel NEAR the crossing is evidence about this bridge.
            reach = channel.intersection(
                unary_union([p.buffer(reach_m) for p in points]))
            if reach.is_empty:
                continue
            flooded = reach.intersection(extent_geom).length
            if flooded > best_len:
                best_len = float(flooded)
                props = waterways.iloc[int(j)]
                best_name = (props.get("name") or props.get("waterway")
                             or props.get("@osmId"))
        if best_len > 0:
            base = bridges.iloc[i].drop(labels="geometry", errors="ignore").to_dict()
            rows.append({**base, "crossing_waterway": best_name,
                         "crossed_channel_flooded_m": round(best_len, 3),
                         "status": BRIDGE_UNVERIFIED_LABEL})
            geometries.append(bridge)
    if not rows:
        return empty
    return gpd.GeoDataFrame(rows, geometry=geometries, crs=bridges.crs)


# --- putting it together ------------------------------------------------------

@dataclass
class DamageResult:
    """Everything the overlay found, plus the numbers for run_report.json."""

    buildings_total: int
    buildings_damaged: int
    segments_total: int
    segments_cut: int
    road_length_total_m: float
    road_length_cut_m: float
    road_length_flooded_m: float
    bridges_total: int
    bridges_likely_out: int
    extent_area_ha: float
    buffer_px: int
    buffer_m: float
    extent_classes: tuple
    by_highway: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)   # name -> GeoDataFrame

    def report(self) -> dict:
        """What goes into run_report.json under `damage`."""
        km = lambda m: round(float(m) / 1000.0, 3)  # noqa: E731
        return {
            "parameters": self.params,
            "extent": {
                "classes": list(self.extent_classes),
                "buffer_px": self.buffer_px,
                "buffer_m": self.buffer_m,
                "buffered_area_ha": self.extent_area_ha,
                "note": ("the overlay uses ExtentParams.flood_extent_classes(), so the "
                         "debris class is consumed only if it is part of the flood extent"),
            },
            "buildings": {"total": self.buildings_total,
                          "damaged": self.buildings_damaged},
            "roads": {"segments_total": self.segments_total,
                      "segments_cut": self.segments_cut,
                      "length_total_km": km(self.road_length_total_m),
                      "length_cut_km": km(self.road_length_cut_m),
                      "length_flooded_km": km(self.road_length_flooded_m),
                      "by_highway": self.by_highway},
            "bridges": {"total": self.bridges_total,
                        "likely_out": self.bridges_likely_out,
                        "status_label": BRIDGE_UNVERIFIED_LABEL},
            "caveat": DAMAGE_CAVEAT,
        }


def _highway_breakdown(segments, cut) -> dict:
    """Per `highway` value: segments and kilometres, total and cut.

    Reported because "12 roads cut" means something different for a trunk road
    than for a footpath, and a rescuer routing around a flood needs to know
    which.
    """
    out: dict[str, dict] = {}
    if len(segments) == 0 or "highway" not in segments.columns:
        return out
    cut_ids = set(cut["segment_id"]) if len(cut) and "segment_id" in cut.columns else set()
    for value, group in segments.groupby(segments["highway"].astype("string"), dropna=True):
        is_cut = (group["segment_id"].isin(cut_ids) if "segment_id" in group.columns
                  else group.index.isin([]))
        out[str(value)] = {
            "segments_total": int(len(group)),
            "segments_cut": int(is_cut.sum()),
            "length_total_km": round(float(group.geometry.length.sum()) / 1000.0, 3),
            "length_cut_km": round(float(group[is_cut].geometry.length.sum()) / 1000.0, 3),
        }
    return dict(sorted(out.items()))


def assess(classes: np.ndarray, grid, osm_layers: dict, params: DamageParams | None = None,
           extent: ExtentParams | None = None, log=print) -> DamageResult:
    """The whole overlay. `osm_layers` maps layer name -> GeoJSON dict.

    Everything is reprojected to the grid's CRS first, so lengths and buffers
    are metres on the analysis grid and never degrees.
    """
    from strandline import osm as osm_mod

    dp = params if params is not None else DamageParams()
    ep = extent if extent is not None else ExtentParams()
    geom = extent_geometry(classes, grid, ep, dp.buffer_px)
    log(f"      flood extent for the overlay: {geom.area / 10_000.0:.1f} ha "
        f"(buffered {dp.buffer_px} px = {dp.buffer_px * grid.res:g} m)")

    crs = grid.crs
    buildings = osm_mod.to_geodataframe(osm_layers.get("buildings") or {}, crs)
    roads = osm_mod.to_geodataframe(osm_layers.get("roads") or {}, crs)
    bridges = osm_mod.to_geodataframe(osm_layers.get("bridges") or {}, crs)
    waterways = osm_mod.to_geodataframe(osm_layers.get("waterways") or {}, crs)

    segments = split_at_junctions(roads, dp.min_segment_m)
    hit_buildings = damaged_buildings(buildings, geom)
    cut = cut_road_segments(segments, geom)
    out_bridges = bridges_likely_out(bridges, waterways, geom, dp.bridge_reach_m)

    log(f"      damage: {len(hit_buildings)}/{len(buildings)} building(s), "
        f"{len(cut)}/{len(segments)} road segment(s), "
        f"{len(out_bridges)}/{len(bridges)} bridge(s) {BRIDGE_UNVERIFIED_LABEL}")

    return DamageResult(
        buildings_total=int(len(buildings)),
        buildings_damaged=int(len(hit_buildings)),
        segments_total=int(len(segments)),
        segments_cut=int(len(cut)),
        road_length_total_m=float(segments.geometry.length.sum()) if len(segments) else 0.0,
        road_length_cut_m=float(cut.geometry.length.sum()) if len(cut) else 0.0,
        road_length_flooded_m=(float(cut["flooded_length_m"].sum()) if len(cut) else 0.0),
        bridges_total=int(len(bridges)),
        bridges_likely_out=int(len(out_bridges)),
        extent_area_ha=round(float(geom.area) / 10_000.0, 2),
        buffer_px=dp.buffer_px,
        buffer_m=float(dp.buffer_px * grid.res),
        extent_classes=tuple(ep.flood_extent_classes()),
        by_highway=_highway_breakdown(segments, cut),
        params=asdict(dp),
        layers={"damaged_buildings": hit_buildings, "cut_roads": cut,
                "bridges_out": out_bridges},
    )


# --- writing the layers -------------------------------------------------------

# Properties kept per layer, in output order. A whole OSM tag set per feature
# would make these files enormous and unreadable; these are the fields a
# dashboard, a sitrep or the copilot actually cites.
KEEP = {
    "damaged_buildings": ("@osmId", "building", "name", "amenity", "healthcare"),
    "cut_roads": ("@osmId", "segment_id", "highway", "name", "bridge",
                  "segment_length_m", "flooded_length_m", "flooded_fraction"),
    "bridges_out": ("@osmId", "bridge", "highway", "railway", "name",
                    "crossing_waterway", "crossed_channel_flooded_m", "status"),
}


def _features(gdf, keep: tuple) -> list[dict]:
    if len(gdf) == 0:
        return []
    out = []
    wgs = gdf.to_crs("EPSG:4326")
    for (_, row), geom in zip(gdf.iterrows(), wgs.geometry):
        props = {}
        for key in keep:
            if key in gdf.columns:
                value = row[key]
                if value is None or (isinstance(value, float) and np.isnan(value)):
                    continue
                props[key] = value.item() if hasattr(value, "item") else value
        out.append({"type": "Feature", "properties": props,
                    "geometry": json.loads(json.dumps(geom.__geo_interface__))})
    return out


def write_layers(out: Path, result: DamageResult, log=print) -> dict[str, int]:
    """Write damaged_buildings, cut_roads and bridges_out as GeoJSON.

    Written even when EMPTY, on purpose: a missing file is ambiguous between
    "nothing was damaged" and "the overlay did not run", and a dashboard must
    be able to tell those apart. Every file carries the caveat.
    """
    written: dict[str, int] = {}
    for name, keep in KEEP.items():
        gdf = result.layers.get(name)
        features = _features(gdf, keep) if gdf is not None else []
        body = {
            "type": "FeatureCollection",
            "properties": {
                "layer": name,
                "caveat": DAMAGE_CAVEAT,
                "flood_extent_classes": list(result.extent_classes),
                "buffer_m": result.buffer_m,
                "attribution": "© OpenStreetMap contributors",
            },
            "features": features,
        }
        if name == "bridges_out":
            body["properties"]["status_label"] = BRIDGE_UNVERIFIED_LABEL
        (out / f"{name}.geojson").write_text(json.dumps(body, indent=1, default=str),
                                             encoding="utf-8")
        written[name] = len(features)
    log(f"      wrote {', '.join(f'{k}.geojson ({v})' for k, v in written.items())}")
    return written
