"""The damage overlay on hand-made geometry whose answers are known by hand.

SYNTHETIC: every polygon, line and flood pixel below is written out here, so
these are tests of the overlay's logic and are never real-world damage accuracy
(Rule 3.8). The real numbers come from `eval/damage_eval.py` into
`eval/results/damage.csv`.

The fixture is a 100 x 100 cell grid at 20 m in a projected CRS, with a flood
strip 10 cells (200 m) wide running down the middle. Everything is positioned
in metres relative to that strip, so each expected answer is arithmetic a
reader can check:

    x metres:  0 ........ 1000 [FLOOD 1000-1200] 1200 ........ 2000

With `buffer_px=1` the flood extent used by the overlay is 980-1220 m.
"""

from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from strandline import damage
from strandline.config import (
    BRIDGE_UNVERIFIED_LABEL,
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NO_CHANGE,
    CLASS_NODATA,
    DamageParams,
    ExtentParams,
)

RES = 20.0
N = 100
CRS = "EPSG:32633"
FLOOD_X0, FLOOD_X1 = 1000.0, 1200.0     # metres from the grid's left edge


class Grid:
    """The few attributes damage.py asks of a grid."""

    def __init__(self):
        self.res = RES
        self.crs = CRS
        self.width = self.height = N
        # Origin at (0, 2000) so x runs 0..2000 and y runs 2000..0 downward.
        self.transform = from_origin(0.0, N * RES, RES, RES)


GRID = Grid()


def classes_with_strip(x0=FLOOD_X0, x1=FLOOD_X1, code=CLASS_FLOOD_MEDIUM):
    """A class raster with one vertical flood strip between x0 and x1 metres."""
    arr = np.full((N, N), CLASS_NO_CHANGE, dtype=np.uint8)
    arr[:, int(x0 / RES):int(x1 / RES)] = code
    return arr


def gdf(geoms, **columns):
    data = {k: v for k, v in columns.items()}
    return gpd.GeoDataFrame(data, geometry=list(geoms), crs=CRS)


# --- the extent geometry ------------------------------------------------------

def test_the_extent_is_the_flood_strip_grown_by_exactly_one_pixel():
    geom = damage.extent_geometry(classes_with_strip(), GRID, ExtentParams(), buffer_px=1)
    w, s, e, n = geom.bounds
    assert (w, e) == pytest.approx((FLOOD_X0 - RES, FLOOD_X1 + RES))
    # A full-height strip grown vertically is clipped by the raster, so its
    # area is width x the full grid height.
    assert geom.area == pytest.approx((FLOOD_X1 - FLOOD_X0 + 2 * RES) * N * RES)


def test_a_zero_buffer_leaves_the_strip_exactly_where_it_is():
    geom = damage.extent_geometry(classes_with_strip(), GRID, ExtentParams(), buffer_px=0)
    assert geom.bounds[0] == pytest.approx(FLOOD_X0)
    assert geom.bounds[2] == pytest.approx(FLOOD_X1)


def test_an_empty_extent_gives_an_empty_geometry_not_an_error():
    arr = np.full((N, N), CLASS_NO_CHANGE, dtype=np.uint8)
    geom = damage.extent_geometry(arr, GRID, ExtentParams(), buffer_px=1)
    assert geom.is_empty
    assert damage.damaged_buildings(gdf([box(0, 0, 10, 10)]), geom).empty


def test_debris_is_not_consumed_unless_it_is_part_of_the_flood_extent():
    """The 2026-10-12 decision: debris is an output layer, not flood extent,
    and must not reach the damage overlay. The flag is what decides, not a
    hardcoded class tuple here."""
    arr = classes_with_strip(code=CLASS_DEBRIS)
    out = damage.extent_geometry(arr, GRID, ExtentParams(debris_in_flood_extent=False), 1)
    assert out.is_empty, "debris reached the overlay while out of the flood extent"
    inside = damage.extent_geometry(arr, GRID, ExtentParams(debris_in_flood_extent=True), 1)
    assert not inside.is_empty


def test_both_water_confidence_classes_are_extent():
    arr = classes_with_strip(code=CLASS_FLOOD_HIGH)
    assert not damage.extent_geometry(arr, GRID, ExtentParams(), 1).is_empty


def test_nodata_is_not_extent():
    arr = np.full((N, N), CLASS_NODATA, dtype=np.uint8)
    assert damage.extent_geometry(arr, GRID, ExtentParams(), 1).is_empty


# --- buildings ----------------------------------------------------------------

EXTENT = damage.extent_geometry(classes_with_strip(), GRID, ExtentParams(), buffer_px=1)


def test_buildings_are_flagged_exactly_when_the_footprint_touches_the_extent():
    """Five buildings with hand-computed answers against the 980-1220 m extent."""
    buildings = gdf(
        [box(1050, 100, 1090, 140),      # wholly inside the strip      -> YES
         box(1180, 200, 1260, 240),      # straddles the strip edge     -> YES
         box(960, 300, 990, 330),        # reaches into the 1 px buffer -> YES
         box(900, 400, 940, 440),        # 40 m clear of the buffer     -> no
         box(1500, 500, 1560, 560)],     # far away                     -> no
        **{"@osmId": ["way/1", "way/2", "way/3", "way/4", "way/5"],
           "building": ["house", "yes", "school", "yes", "yes"]})
    flagged = damage.damaged_buildings(buildings, EXTENT)
    assert sorted(flagged["@osmId"]) == ["way/1", "way/2", "way/3"]


def test_the_one_pixel_buffer_is_what_catches_the_near_miss():
    """Without the buffer, `way/3` at 960-990 m would not be flagged.

    The buffer is a geolocation tolerance, so this test pins what it buys -
    and the zero-buffer case pins that it is the buffer doing it, not the
    intersection test being loose.
    """
    near = gdf([box(960, 300, 990, 330)], **{"@osmId": ["way/3"]})
    unbuffered = damage.extent_geometry(classes_with_strip(), GRID, ExtentParams(), 0)
    assert damage.damaged_buildings(near, unbuffered).empty
    assert len(damage.damaged_buildings(near, EXTENT)) == 1


def test_no_buildings_in_means_none_out():
    assert damage.damaged_buildings(gdf([]), EXTENT).empty


# --- road segments ------------------------------------------------------------

def test_roads_are_split_at_junctions_into_the_expected_segments():
    """A 2 km east-west road crossed by two north-south roads -> 3 segments.

    The crossing roads are split too (one each side), so the whole set is
    3 + 2 + 2 = 7 segments.
    """
    roads = gdf(
        [LineString([(0, 1000), (2000, 1000)]),      # main, crossed twice
         LineString([(600, 400), (600, 1600)]),      # crossing 1
         LineString([(1400, 400), (1400, 1600)])],   # crossing 2
        **{"@osmId": ["way/main", "way/c1", "way/c2"],
           "highway": ["primary", "residential", "track"]})
    segments = damage.split_at_junctions(roads, min_segment_m=5.0)
    assert len(segments) == 7
    main = segments[segments["@osmId"] == "way/main"]
    assert len(main) == 3
    assert sorted(round(v) for v in main["segment_length_m"]) == [600, 600, 800]
    assert set(main["segment_id"]) == {"way/main#0", "way/main#1", "way/main#2"}
    assert set(main["highway"]) == {"primary"}, "tags must survive the split"


def test_an_uncrossed_road_is_left_as_one_segment():
    roads = gdf([LineString([(0, 100), (500, 100)])], **{"@osmId": ["way/lone"]})
    segments = damage.split_at_junctions(roads)
    assert len(segments) == 1
    assert segments.iloc[0]["segment_length_m"] == pytest.approx(500.0)


def test_sliver_fragments_below_the_minimum_are_dropped():
    """Two ways meeting 1 m from an endpoint must not create a 1 m 'segment'."""
    roads = gdf([LineString([(0, 100), (500, 100)]),
                 LineString([(1, 0), (1, 200)])],
                **{"@osmId": ["way/a", "way/b"]})
    segments = damage.split_at_junctions(roads, min_segment_m=5.0)
    assert all(segments["segment_length_m"] >= 5.0)
    kept = segments[segments["@osmId"] == "way/a"]
    assert len(kept) == 1, "the 1 m stub should be dropped, the 499 m piece kept"


def test_a_segment_is_cut_only_when_part_of_it_is_inside_and_the_length_is_right():
    """Three segments, each answer computed by hand against 980-1220 m."""
    segments = damage.split_at_junctions(
        gdf([LineString([(0, 1000), (2000, 1000)]),      # crosses: 240 m flooded
             LineString([(0, 500), (900, 500)]),         # stops 80 m short: not cut
             LineString([(1500, 200), (1900, 200)])],    # far east: not cut
            **{"@osmId": ["way/cross", "way/short", "way/east"],
               "highway": ["primary", "residential", "track"]}))
    cut = damage.cut_road_segments(segments, EXTENT)
    assert list(cut["@osmId"]) == ["way/cross"]
    assert cut.iloc[0]["flooded_length_m"] == pytest.approx(240.0)
    assert cut.iloc[0]["flooded_fraction"] == pytest.approx(240.0 / 2000.0, abs=1e-4)


def test_a_road_merely_tangent_to_the_extent_boundary_is_not_cut():
    """The 20 m buffer already carries the tolerance; a tangent is not water
    on the road, and counting it would inflate every cut count."""
    tangent = gdf([LineString([(0, 100), (FLOOD_X0 - RES, 100)])],
                  **{"@osmId": ["way/t"], "segment_id": ["way/t#0"]})
    assert damage.cut_road_segments(tangent, EXTENT).empty


def test_flooded_length_is_reported_not_just_the_whole_segment_length():
    """An 8 km road with 240 m under water must not be reported as 8 km lost."""
    long_road = damage.split_at_junctions(
        gdf([LineString([(0, 1000), (8000, 1000)])], **{"@osmId": ["way/long"]}))
    cut = damage.cut_road_segments(long_road, EXTENT)
    assert cut.iloc[0]["segment_length_m"] == pytest.approx(8000.0)
    assert cut.iloc[0]["flooded_length_m"] == pytest.approx(240.0)
    assert cut.iloc[0]["flooded_fraction"] < 0.05


def test_no_roads_in_means_none_out():
    assert damage.cut_road_segments(gdf([]), EXTENT).empty
    assert damage.split_at_junctions(gdf([])).empty


# --- bridges ------------------------------------------------------------------

def test_a_bridge_over_the_flooded_channel_is_flagged_as_unverified():
    river = gdf([LineString([(1100, 0), (1100, 2000)])],
                **{"@osmId": ["way/river"], "waterway": ["river"], "name": ["Test River"]})
    bridges = gdf([LineString([(1050, 800), (1150, 800)])],
                  **{"@osmId": ["way/bridge"], "bridge": ["yes"], "highway": ["primary"]})
    out = damage.bridges_likely_out(bridges, river, EXTENT, reach_m=100.0)
    assert len(out) == 1
    assert out.iloc[0]["status"] == BRIDGE_UNVERIFIED_LABEL
    assert out.iloc[0]["crossing_waterway"] == "Test River"
    assert out.iloc[0]["crossed_channel_flooded_m"] > 0


def test_a_bridge_crossing_no_waterway_is_not_flagged():
    """A flyover inside the flood extent crosses a road, not a channel."""
    river = gdf([LineString([(1100, 0), (1100, 300)])],
                **{"@osmId": ["way/river"], "waterway": ["stream"]})
    flyover = gdf([LineString([(1050, 1800), (1150, 1800)])],
                  **{"@osmId": ["way/fly"], "bridge": ["yes"]})
    assert damage.bridges_likely_out(flyover, river, EXTENT, 100.0).empty


def test_a_bridge_over_a_dry_channel_is_not_flagged():
    """The channel exists and is crossed, but no part of it is in the extent."""
    dry = gdf([LineString([(1600, 0), (1600, 2000)])],
              **{"@osmId": ["way/dry"], "waterway": ["stream"]})
    bridges = gdf([LineString([(1550, 800), (1650, 800)])],
                  **{"@osmId": ["way/b"], "bridge": ["yes"]})
    assert damage.bridges_likely_out(bridges, dry, EXTENT, 100.0).empty


def test_flooding_far_down_the_same_river_does_not_condemn_this_crossing():
    """The reach is what makes the bridge rule about THIS crossing.

    The river runs the full height of the grid and is flooded where it passes
    the strip; the bridge crosses it 1.5 km to the east, over dry ground. With
    a 100 m reach the bridge is not flagged; with an absurd 5 km reach it is,
    which shows the reach is doing the work.
    """
    river = gdf([LineString([(0, 1000), (2000, 1000)])],
                **{"@osmId": ["way/r"], "waterway": ["river"]})
    bridge = gdf([LineString([(1700, 900), (1700, 1100)])],
                 **{"@osmId": ["way/b"], "bridge": ["yes"]})
    assert damage.bridges_likely_out(bridge, river, EXTENT, reach_m=100.0).empty
    assert len(damage.bridges_likely_out(bridge, river, EXTENT, reach_m=5000.0)) == 1


def test_no_bridges_or_no_waterways_means_none_flagged():
    river = gdf([LineString([(1100, 0), (1100, 2000)])], **{"waterway": ["river"]})
    bridges = gdf([LineString([(1050, 800), (1150, 800)])], **{"bridge": ["yes"]})
    assert damage.bridges_likely_out(gdf([]), river, EXTENT, 100.0).empty
    assert damage.bridges_likely_out(bridges, gdf([]), EXTENT, 100.0).empty


# --- the whole assessment and its outputs -------------------------------------

def _layers():
    """The hand-made OSM layers as GeoJSON, as osm.load_snapshot would give."""
    def fc(geoms, props):
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": p,
             "geometry": json.loads(json.dumps(g.__geo_interface__))}
            for g, p in zip(geoms, props)]}

    # Coordinates are METRES in the grid's own CRS, not degrees, which is what
    # makes every expected answer below checkable by hand. `assess` normally
    # reprojects from WGS84, so the `assess_result` fixture replaces that one
    # reader with a no-op rather than restating the fixture in degrees.
    return (
        fc([box(1050, 100, 1090, 140), box(900, 400, 940, 440)],
           [{"@osmId": "way/b1", "building": "house"},
            {"@osmId": "way/b2", "building": "yes"}]),
        fc([LineString([(0, 1000), (2000, 1000)]), LineString([(1500, 200), (1900, 200)])],
           [{"@osmId": "way/r1", "highway": "primary"},
            {"@osmId": "way/r2", "highway": "track"}]),
        fc([LineString([(1050, 800), (1150, 800)])],
           [{"@osmId": "way/br", "bridge": "yes", "highway": "primary"}]),
        fc([LineString([(1100, 0), (1100, 2000)])],
           [{"@osmId": "way/w", "waterway": "river"}]),
    )


@pytest.fixture
def assess_result(monkeypatch):
    """assess() on the hand-made layers, with the CRS step made a no-op.

    The fixture geometry is already in the grid's CRS - that is what makes the
    expected metres checkable by hand - so the reprojection inside assess is
    bypassed rather than the fixture being rewritten in degrees.
    """
    buildings, roads, bridges, waterways = _layers()
    from strandline import osm as osm_mod

    def fake_to_gdf(body, crs_to=None):
        features = list((body or {}).get("features") or [])
        if not features:
            return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=CRS)
        return gpd.GeoDataFrame.from_features(features, crs=CRS)

    monkeypatch.setattr(osm_mod, "to_geodataframe", fake_to_gdf)
    return damage.assess(
        classes_with_strip(), GRID,
        {"buildings": buildings, "roads": roads, "bridges": bridges,
         "waterways": waterways},
        DamageParams(), ExtentParams(), log=lambda *a: None)


def test_assess_counts_match_the_hand_computed_answers(assess_result):
    r = assess_result
    assert (r.buildings_total, r.buildings_damaged) == (2, 1)
    # The main road is split by the bridge way and the river crossing it, so
    # the segment count is >= 2; exactly one carries flood water.
    assert r.segments_cut == 1
    assert r.road_length_flooded_m == pytest.approx(240.0)
    assert (r.bridges_total, r.bridges_likely_out) == (1, 1)
    assert r.buffer_px == 1 and r.buffer_m == pytest.approx(20.0)
    assert CLASS_DEBRIS not in r.extent_classes


def test_the_report_carries_counts_lengths_parameters_and_the_caveat(assess_result):
    report = assess_result.report()
    assert report["buildings"] == {"total": 2, "damaged": 1}
    assert report["roads"]["segments_cut"] == 1
    assert report["roads"]["length_flooded_km"] == pytest.approx(0.24)
    assert report["bridges"]["likely_out"] == 1
    assert report["bridges"]["status_label"] == BRIDGE_UNVERIFIED_LABEL
    assert report["parameters"]["buffer_px"] == 1
    assert report["parameters"]["bridge_reach_m"] == 100.0
    assert "inherits the flood map's accuracy" in report["caveat"]
    assert "primary" in report["roads"]["by_highway"]


def test_the_highway_breakdown_separates_a_trunk_road_from_a_track(assess_result):
    by_highway = assess_result.report()["roads"]["by_highway"]
    assert by_highway["primary"]["segments_cut"] >= 1
    assert by_highway["track"]["segments_cut"] == 0
    assert by_highway["track"]["segments_total"] == 1


def test_the_three_layers_are_written_even_when_empty(tmp_path, assess_result):
    """A missing file cannot distinguish 'no damage' from 'did not run'."""
    written = damage.write_layers(tmp_path, assess_result, log=lambda *a: None)
    assert set(written) == {"damaged_buildings", "cut_roads", "bridges_out"}
    for name in written:
        body = json.loads((tmp_path / f"{name}.geojson").read_text(encoding="utf-8"))
        assert body["type"] == "FeatureCollection"
        assert "caveat" in body["properties"]
        assert body["properties"]["attribution"] == "© OpenStreetMap contributors"
    bridges = json.loads((tmp_path / "bridges_out.geojson").read_text(encoding="utf-8"))
    assert bridges["properties"]["status_label"] == BRIDGE_UNVERIFIED_LABEL
    assert bridges["features"][0]["properties"]["status"] == BRIDGE_UNVERIFIED_LABEL


def test_written_features_are_in_wgs84_with_the_cited_properties(tmp_path, assess_result):
    damage.write_layers(tmp_path, assess_result, log=lambda *a: None)
    roads = json.loads((tmp_path / "cut_roads.geojson").read_text(encoding="utf-8"))
    props = roads["features"][0]["properties"]
    assert {"segment_id", "flooded_length_m", "highway"} <= set(props)
    lon, lat = roads["features"][0]["geometry"]["coordinates"][0]
    assert -180 <= lon <= 180 and -90 <= lat <= 90


def test_an_empty_flood_extent_reports_zeros_rather_than_failing(monkeypatch):
    buildings, roads, bridges, waterways = _layers()
    from strandline import osm as osm_mod
    monkeypatch.setattr(osm_mod, "to_geodataframe",
                        lambda body, crs_to=None: (
                            gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=CRS)
                            if not (body or {}).get("features")
                            else gpd.GeoDataFrame.from_features(body["features"], crs=CRS)))
    dry = np.full((N, N), CLASS_NO_CHANGE, dtype=np.uint8)
    r = damage.assess(dry, GRID, {"buildings": buildings, "roads": roads,
                                  "bridges": bridges, "waterways": waterways},
                      DamageParams(), ExtentParams(), log=lambda *a: None)
    assert (r.buildings_damaged, r.segments_cut, r.bridges_likely_out) == (0, 0, 0)
    assert r.buildings_total == 2, "the denominator must still be reported"
    assert r.extent_area_ha == 0.0


# --- parameters ---------------------------------------------------------------

def test_the_damage_parameters_do_not_touch_the_flood_params_hash():
    """DamageParams changes no pixel of the class map, so folding it into the
    hash would re-key every row already published in eval/results/."""
    from strandline.config import DetectParams, RunParams, params_dict
    from strandline.run import params_hash

    sections = params_dict(DetectParams(), RunParams())
    assert "damage" not in sections and "osm" not in sections
    assert params_hash(DetectParams(), RunParams()) == params_hash(DetectParams(), RunParams())


@pytest.mark.parametrize("kwargs", [
    {"buffer_px": -1}, {"bridge_reach_m": 0.0}, {"bridge_reach_m": -5.0},
    {"min_segment_m": -1.0},
])
def test_implausible_damage_parameters_are_refused(kwargs):
    with pytest.raises(ValueError):
        DamageParams(**kwargs)


# --- the pipeline stage -------------------------------------------------------

class _Scene:
    def __init__(self):
        self.grid = GRID


def test_the_stage_records_disabled_when_the_overlay_is_not_asked_for():
    from strandline import run as pipeline

    class R:
        classes = classes_with_strip()

    assessment, report = pipeline.damage_stage(
        (1, 2, 3, 4), __import__("datetime").date(2024, 10, 4), _Scene(), R(),
        __import__("pathlib").Path("."), None, None, log=lambda *a: None)
    assert assessment is None
    assert report["status"] == pipeline.OSM_DISABLED
    assert "--no-damage" in report["message"]


def test_an_osm_refusal_does_not_lose_the_run_and_says_there_is_no_answer(monkeypatch,
                                                                         tmp_path):
    """Rule 5 and Rule 6: a flood map must survive an OSM failure, and a
    refusal must never be reported as zero damage."""
    from strandline import osm as osm_mod
    from strandline import run as pipeline
    from strandline.config import OsmParams

    def boom(*a, **k):
        raise osm_mod.OsmError("ohsome is unreachable; try again")

    monkeypatch.setattr(osm_mod, "load_snapshot", boom)

    class R:
        classes = classes_with_strip()

    assessment, report = pipeline.damage_stage(
        (1, 2, 3, 4), __import__("datetime").date(2024, 10, 4), _Scene(), R(), tmp_path,
        OsmParams(), DamageParams(), log=lambda *a: None)
    assert assessment is None
    assert report["status"] == pipeline.OSM_REFUSED
    assert report["damage_available"] is False
    assert "unreachable" in report["message"]
    # No count of any kind, so nothing downstream can read a zero out of it.
    assert "buildings" not in report and "roads" not in report


def test_a_successful_stage_reports_the_snapshot_beside_the_counts(monkeypatch, tmp_path):
    from strandline import osm as osm_mod
    from strandline import run as pipeline
    from strandline.config import OsmParams

    buildings, roads, bridges, waterways = _layers()
    layers = {"buildings": buildings, "roads": roads, "bridges": bridges,
              "waterways": waterways}

    class FakeSnapshot:
        def report(self):
            return {"snapshot": "2024-10-03", "buffer_km": 15.0}

    fake = FakeSnapshot()
    fake.layers = layers
    monkeypatch.setattr(osm_mod, "load_snapshot", lambda *a, **k: fake)
    monkeypatch.setattr(osm_mod, "to_geodataframe",
                        lambda body, crs_to=None: (
                            gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=CRS)
                            if not (body or {}).get("features")
                            else gpd.GeoDataFrame.from_features(body["features"], crs=CRS)))

    class R:
        classes = classes_with_strip()

    assessment, report = pipeline.damage_stage(
        (1, 2, 3, 4), __import__("datetime").date(2024, 10, 4), _Scene(), R(), tmp_path,
        OsmParams(), DamageParams(), log=lambda *a: None)
    assert assessment is not None
    assert report["status"] == pipeline.OSM_USED
    assert report["damage_available"] is True
    assert report["osm"]["snapshot"] == "2024-10-03"
    assert report["buildings"]["damaged"] == 1
    assert (tmp_path / "cut_roads.geojson").exists()
