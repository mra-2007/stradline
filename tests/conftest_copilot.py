"""A SYNTHETIC run folder for the copilot tests.

Hand-written, tiny, and labelled synthetic wherever a number from it is
reported (Rule 6): nothing here is a measurement of anything, it exists so the
copilot's logic can be tested without a satellite download. The real numbers
the copilot is scored on come from `eval/copilot_eval.py` over real run
folders.

The shape of every file matches exactly what `strandline/outputs.py`,
`strandline/damage.py` and `strandline/network.py` actually write.
"""

from __future__ import annotations

import json
from pathlib import Path

from strandline.config import (
    BRIDGE_UNVERIFIED_LABEL,
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NAMES,
    CLASS_NO_CHANGE,
    CLASS_PERMANENT_WATER,
    CLASS_TERRAIN_EXCLUDED,
    DEBRIS_UNVERIFIED_LABEL,
    NARROW_VALLEY_CAVEAT,
    SCENARIO_BRIDGES_FAILED,
    SCENARIO_BRIDGES_INTACT,
    STATUS_CUT_OFF,
    STATUS_DETOUR,
    STATUS_NO_BASELINE,
    STATUS_REACHABLE,
)

SYNTHETIC_NOTE = "SYNTHETIC test fixture - not a measurement of any real event"


def _settlement(name, kind, status, probability, before, after_intact,
                after_failed=None, n_buildings=None):
    props = {
        "name": name, "kind": kind, "source": "osm_place", "status": status,
        "cut_off_probability": probability,
        "probability_is": "share of a perturbed ensemble, a sensitivity measure, "
                          "NOT a calibrated chance",
        "vehicle_health_before_m": before,
        f"vehicle_health_{SCENARIO_BRIDGES_INTACT}_after_m": after_intact,
        f"vehicle_health_{SCENARIO_BRIDGES_INTACT}_status": status,
        f"vehicle_health_{SCENARIO_BRIDGES_FAILED}_after_m":
            after_failed if after_failed is not None else after_intact,
        f"vehicle_health_{SCENARIO_BRIDGES_FAILED}_status": status,
        "vehicle_town_before_m": before,
        f"vehicle_town_{SCENARIO_BRIDGES_INTACT}_after_m": after_intact,
        f"vehicle_town_{SCENARIO_BRIDGES_INTACT}_status": status,
        f"vehicle_town_{SCENARIO_BRIDGES_FAILED}_after_m":
            after_failed if after_failed is not None else after_intact,
        f"vehicle_town_{SCENARIO_BRIDGES_FAILED}_status": status,
        "foot_health_before_m": before,
        f"foot_health_{SCENARIO_BRIDGES_INTACT}_after_m": after_intact,
        f"foot_health_{SCENARIO_BRIDGES_INTACT}_status": status,
        f"foot_health_{SCENARIO_BRIDGES_FAILED}_after_m":
            after_failed if after_failed is not None else after_intact,
        f"foot_health_{SCENARIO_BRIDGES_FAILED}_status": status,
        "foot_town_before_m": before,
        f"foot_town_{SCENARIO_BRIDGES_INTACT}_after_m": after_intact,
        f"foot_town_{SCENARIO_BRIDGES_INTACT}_status": status,
        f"foot_town_{SCENARIO_BRIDGES_FAILED}_after_m":
            after_failed if after_failed is not None else after_intact,
        f"foot_town_{SCENARIO_BRIDGES_FAILED}_status": status,
    }
    if n_buildings is not None:
        props["n_buildings"] = n_buildings
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [85.2, 28.0]}}


SETTLEMENTS = [
    _settlement("Khanigaun", "village", STATUS_CUT_OFF, 0.92, 4200.0, None, None, 34),
    _settlement("Saurpani", "village", STATUS_DETOUR, 0.41, 5000.0, 12500.0, 18000.0, 22),
    _settlement("Dhunge", "hamlet", STATUS_REACHABLE, 0.05, 2600.0, 2600.0, 2600.0, 9),
    _settlement("Barpak", "village", STATUS_NO_BASELINE, 0.0, None, None, None, 15),
    _settlement("Dhunge", "hamlet", STATUS_CUT_OFF, 0.77, 3100.0, None, None, 6),
]

REPORT = {
    "event_date": "2024-07-15",
    "bbox_wgs84": [85.10, 27.85, 85.20, 27.95],
    "grid": {"epsg": 32645, "resolution_m": 20.0, "width": 100, "height": 100},
    "track": {
        "relative_orbit": 85, "direction": "ASCENDING",
        "post": {"day": "2024-07-18", "scenes": ["S1A_IW_GRDH_SYNTHETIC.SAFE"]},
        "pre": [{"day": "2024-06-12", "scenes": []}, {"day": "2024-06-24", "scenes": []},
                {"day": "2024-07-06", "scenes": []}],
        "post_latency_days": 3,
    },
    "parameters": {}, "parameters_hash": "0123456789ab",
    "areas_ha": {
        CLASS_NAMES[CLASS_NO_CHANGE]: 1200.0,
        CLASS_NAMES[CLASS_FLOOD_HIGH]: 40.0,
        CLASS_NAMES[CLASS_FLOOD_MEDIUM]: 60.0,
        CLASS_NAMES[CLASS_DEBRIS]: 12.5,
        CLASS_NAMES[CLASS_PERMANENT_WATER]: 30.0,
        CLASS_NAMES[CLASS_TERRAIN_EXCLUDED]: 2657.5,
    },
    "runtime_s": 11.0, "n_polygons": 7,
    "flood_extent": {
        "debris_in_flood_extent": False,
        "classes": [CLASS_NAMES[CLASS_FLOOD_HIGH], CLASS_NAMES[CLASS_FLOOD_MEDIUM]],
        "flood_extent_ha": 100.0, "debris_ha": 12.5,
        "debris_label": DEBRIS_UNVERIFIED_LABEL,
    },
    "optical": {"status": "used", "enabled": True, "fused_into_map": True,
                "message": None, "fusion": "union"},
    "reliability": {"narrow_valley": {
        "narrow_valley_ha": 55.0, "flood_affected_ha_in_narrow_valley": 8.0,
        "flood_affected_fraction_in_narrow_valley": 0.08,
        "parameters": {"min_valley_width_px": 3.0}}},
    "damage": {
        "status": "used", "damage_available": True,
        "osm": {"snapshot": "2024-07-14",
                "snapshot_rule": "latest date strictly before the event date",
                "feature_counts": {}, "layers": {}},
        "buildings": {"total": 800, "damaged": 24},
        "roads": {"segments_total": 450, "segments_cut": 12,
                  "length_total_km": 210.5, "length_cut_km": 7.25,
                  "length_flooded_km": 3.4, "by_highway": {}},
        "bridges": {"total": 18, "likely_out": 3,
                    "status_label": BRIDGE_UNVERIFIED_LABEL},
        "caveat": "Damage is an overlay of this run's flood extent on a pre-event "
                  "OpenStreetMap snapshot, not an inspection.",
    },
    "cutoff": {
        "status": "used", "cutoff_available": True,
        "definition": {"gated_definition": "graph vehicle, destination health, "
                                           "scenario bridges_intact"},
        "settlements": {"total": 5, "not_attached_to_any_graph": 1,
                        "by_kind": {"village": 3, "hamlet": 2}},
        "destinations": {"health": 2, "town": 1},
        "graphs": {"vehicle": {"nodes": 90, "edges": 120, "length_km": 61.4},
                   "foot": {"nodes": 140, "edges": 210, "length_km": 98.2}},
        "cut_off_by_the_gated_definition": 2,
        "probability": {"runs": 200, "mean": 0.43, "at_or_above_0.5": 2,
                        "what_it_is": "share of a perturbed ensemble in which the "
                                      "settlement is cut off by the gated definition"},
        "caveat": "Cut-off status is computed from this run's own flood extent and a "
                  "pre-event OpenStreetMap road network.",
    },
    "attribution": ["Contains modified Copernicus Sentinel data 2026."],
}

DAMAGED_BUILDINGS = [
    {"type": "Feature", "properties": {"@osmId": f"way/{100 + i}", "building": "yes"},
     "geometry": {"type": "Point", "coordinates": [85.15, 27.90]}}
    for i in range(24)
]

CUT_ROADS = [
    {"type": "Feature",
     "properties": {"@osmId": "way/5001", "segment_id": "way/5001#0",
                    "highway": "primary", "name": "Pasang Lhamu Highway",
                    "segment_length_m": 2400.0, "flooded_length_m": 1250.0,
                    "flooded_fraction": 0.52},
     "geometry": {"type": "LineString", "coordinates": [[85.1, 27.9], [85.2, 27.9]]}},
    {"type": "Feature",
     "properties": {"@osmId": "way/5002", "segment_id": "way/5002#0",
                    "highway": "residential", "segment_length_m": 900.0,
                    "flooded_length_m": 310.0, "flooded_fraction": 0.34},
     "geometry": {"type": "LineString", "coordinates": [[85.1, 27.91], [85.2, 27.91]]}},
]

BRIDGES_OUT = [
    {"type": "Feature",
     "properties": {"@osmId": "way/9001", "bridge": "yes", "highway": "primary",
                    "name": "Trisuli Bazar Bridge", "status": BRIDGE_UNVERIFIED_LABEL},
     "geometry": {"type": "LineString", "coordinates": [[85.1, 27.92], [85.11, 27.92]]}},
    {"type": "Feature",
     "properties": {"@osmId": "way/9002", "bridge": "yes", "highway": "track",
                    "status": BRIDGE_UNVERIFIED_LABEL},
     "geometry": {"type": "LineString", "coordinates": [[85.12, 27.92], [85.13, 27.92]]}},
    {"type": "Feature",
     "properties": {"@osmId": "way/9003", "bridge": "yes", "railway": "rail",
                    "status": BRIDGE_UNVERIFIED_LABEL},
     "geometry": {"type": "LineString", "coordinates": [[85.14, 27.92], [85.15, 27.92]]}},
]


def _collection(layer: str, features: list) -> dict:
    return {"type": "FeatureCollection",
            "properties": {"layer": layer, "caveat": SYNTHETIC_NOTE},
            "features": features}


def write_run(path: Path, *, with_damage: bool = True, with_cutoff: bool = True,
              damage_status: str = "used") -> Path:
    """Write a complete synthetic run folder and return it."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    report = json.loads(json.dumps(REPORT))
    if not with_damage:
        report["damage"] = {"status": "refused", "damage_available": False,
                            "message": "ohsome could not be reached from this machine"}
    elif damage_status != "used":
        report["damage"] = {"status": damage_status, "damage_available": False,
                            "message": "the overlay broke on a geometry error"}
    if not with_cutoff:
        report["cutoff"] = {"status": "refused", "cutoff_available": False,
                            "message": "no OpenStreetMap snapshot for this run"}
    (path / "run_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (path / "flood_polygons.geojson").write_text(
        json.dumps(_collection("flood", [])), encoding="utf-8")
    (path / "reliability_narrow_valley.geojson").write_text(
        json.dumps(_collection("reliability_narrow_valley",
                               [{"type": "Feature",
                                 "properties": {"caveat": NARROW_VALLEY_CAVEAT,
                                                "area_ha": 55.0},
                                 "geometry": {"type": "Point",
                                              "coordinates": [85.15, 27.9]}}])),
        encoding="utf-8")
    if with_damage and damage_status == "used":
        (path / "damaged_buildings.geojson").write_text(
            json.dumps(_collection("damaged_buildings", DAMAGED_BUILDINGS)),
            encoding="utf-8")
        (path / "cut_roads.geojson").write_text(
            json.dumps(_collection("cut_roads", CUT_ROADS)), encoding="utf-8")
        (path / "bridges_out.geojson").write_text(
            json.dumps(_collection("bridges_out", BRIDGES_OUT)), encoding="utf-8")
    if with_cutoff:
        (path / "settlements.geojson").write_text(
            json.dumps(_collection("settlements", SETTLEMENTS)), encoding="utf-8")
        (path / "routes.geojson").write_text(
            json.dumps(_collection("routes", [])), encoding="utf-8")
    return path
