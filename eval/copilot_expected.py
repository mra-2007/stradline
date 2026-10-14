"""What the right answer IS, computed from a run folder - independently.

This module does NOT import `copilot`. It reads `run_report.json` and the
GeoJSON layers itself and re-derives every value the copilot claims, from the
raw files, with its own arithmetic. That is the whole point: a scoreboard
written on top of the code it is scoring measures nothing (Rule 3), and the
derived numbers - the percentages, the metre-to-kilometre conversions, the
counts over a filtered list - are exactly where a copilot could be quietly
wrong.

What IS shared is the declared contract, and only the contract: the class
names, the gated cut-off definition and `DISPLAY_PRECISION`, all from
`strandline/config.py`. Those are the published definitions of what is being
measured; re-typing them here would let the two sides drift and would measure
agreement with a copy rather than with the contract.

Rule 1: run folders only. No reference map, no EMS product, no published
damage layer is read here or anywhere in the copilot evaluation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from strandline.config import (
    CLASS_DEBRIS,
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NAMES,
    CLASS_NO_CHANGE,
    CLASS_PERMANENT_WATER,
    CLASS_TERRAIN_EXCLUDED,
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    CUTOFF_TARGETS,
    DEVANAGARI_DIGITS,
    DISPLAY_PRECISION,
    GRAPH_KINDS,
    SCENARIOS,
    STATUS_CUT_OFF,
    STATUS_DETOUR,
    STATUS_NO_BASELINE,
)

_TO_DEV = {ord(str(i)): DEVANAGARI_DIGITS[i] for i in range(10)}
TEXT_UNITS = ("date", "text", "id", "class", "status")


def show(value, unit, lang="en") -> str:
    """Format a value the way the contract says it is shown. Independent
    implementation of the same declared precision table."""
    if value is None:
        return "—"
    if unit in TEXT_UNITS or isinstance(value, str):
        text = str(value)
        if lang != "ne" or unit != "date":
            return text
    else:
        text = f"{float(value):.{DISPLAY_PRECISION.get(unit, 0)}f}"
    return text.translate(_TO_DEV) if lang == "ne" else text


class RunFiles:
    """The raw contents of one run folder, and nothing derived."""

    def __init__(self, run_dir):
        self.dir = Path(run_dir)
        self.report = json.loads((self.dir / "run_report.json").read_text(encoding="utf-8"))
        self.layers = {}
        for name in ("settlements.geojson", "cut_roads.geojson", "bridges_out.geojson",
                     "damaged_buildings.geojson", "routes.geojson",
                     "flood_polygons.geojson"):
            path = self.dir / name
            if path.exists():
                self.layers[name] = json.loads(path.read_text(encoding="utf-8"))

    # --- what this run can and cannot answer ------------------------------

    @property
    def has_damage(self) -> bool:
        return ((self.report.get("damage") or {}).get("status") == "used"
                and all(n in self.layers for n in ("damaged_buildings.geojson",
                                                   "cut_roads.geojson",
                                                   "bridges_out.geojson")))

    @property
    def has_cutoff(self) -> bool:
        return ((self.report.get("cutoff") or {}).get("status") == "used"
                and "settlements.geojson" in self.layers)

    def features(self, layer: str) -> list:
        return list((self.layers.get(layer) or {}).get("features") or [])

    def properties(self, layer: str) -> list:
        return [f.get("properties") or {} for f in self.features(layer)]

    # --- the raw numbers --------------------------------------------------

    def area(self, class_code) -> float:
        return float((self.report.get("areas_ha") or {}).get(CLASS_NAMES[class_code], 0.0))

    @property
    def extent_ha(self) -> float:
        extent = self.report.get("flood_extent") or {}
        if extent.get("flood_extent_ha") is not None:
            return float(extent["flood_extent_ha"])
        return round(self.area(CLASS_FLOOD_HIGH) + self.area(CLASS_FLOOD_MEDIUM), 3)

    @property
    def debris_ha(self) -> float:
        extent = self.report.get("flood_extent") or {}
        if extent.get("debris_ha") is not None:
            return float(extent["debris_ha"])
        return self.area(CLASS_DEBRIS)

    @property
    def mapped_ha(self) -> float:
        return round(sum(float(v) for v in (self.report.get("areas_ha") or {}).values()), 3)

    @property
    def analysed_ha(self) -> float:
        return round(self.mapped_ha - self.area(CLASS_TERRAIN_EXCLUDED), 3)

    def damage(self, *keys):
        node = self.report.get("damage") or {}
        for key in keys:
            node = (node or {}).get(key)
        return node

    def cutoff(self, *keys):
        node = self.report.get("cutoff") or {}
        for key in keys:
            node = (node or {}).get(key)
        return node

    # --- settlements ------------------------------------------------------

    def settlement_names(self) -> list:
        return [str(p.get("name")) for p in self.properties("settlements.geojson")]

    def gated_status(self, props: dict):
        return props.get("status")

    def cut_off_names(self) -> list:
        return [str(p.get("name")) for p in self.properties("settlements.geojson")
                if p.get("status") == STATUS_CUT_OFF]

    def lost_route_indices(self, graph=CUTOFF_GATE_GRAPH, target=CUTOFF_GATE_TARGET,
                           scenario=CUTOFF_GATE_SCENARIO) -> list:
        key = f"{graph}_{target}_{scenario}_status"
        return [i for i, p in enumerate(self.properties("settlements.geojson"))
                if p.get(key) == STATUS_CUT_OFF]

    def detour_indices(self, graph=CUTOFF_GATE_GRAPH, target=CUTOFF_GATE_TARGET,
                       scenario=CUTOFF_GATE_SCENARIO) -> list:
        key = f"{graph}_{target}_{scenario}_status"
        return [i for i, p in enumerate(self.properties("settlements.geojson"))
                if p.get(key) == STATUS_DETOUR]

    def pick_settlement(self) -> tuple:
        """A settlement to ask about: the first CUT OFF one, else the first
        one with a route, else the first at all. Deterministic, and chosen
        from the run's own layer so the question is always answerable."""
        props = self.properties("settlements.geojson")
        if not props:
            return None, None
        names = [str(p.get("name")) for p in props]
        unique = [i for i, name in enumerate(names) if names.count(name) == 1]
        for wanted in (STATUS_CUT_OFF, STATUS_DETOUR, None):
            for i in unique:
                if wanted is None or props[i].get("status") == wanted:
                    return i, names[i]
        return (unique[0], names[unique[0]]) if unique else (0, names[0])


# --- the independent fact table --------------------------------------------
#
# fact id -> (value, unit), re-derived from the files. A fact id the evaluation
# cannot compute returns None, and the harness then treats any number the
# copilot attributed to it as UNVERIFIED - the safe direction.

_SETTLEMENT_RE = re.compile(r"^settlement\.(\d+)\.(.+)$")
_ROAD_RE = re.compile(r"^road\.(.+)\.(name|flooded_length_m|flooded_length_km)$")
_BRIDGE_RE = re.compile(r"^bridge\.(.+)\.name$")
_LOST_RE = re.compile(r"^lost_access\.(\w+)_(\w+?)_(bridges_\w+)\.(lost_count|detour_count)$")
_QUERY_RE = re.compile(r"^query\.(.+)\.match_count$")


def _pct(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return round(100.0 * float(numerator) / float(denominator), 4)


def fact(files: RunFiles, fact_id: str):
    """(value, unit) for one fact id, computed from the run files."""
    simple = _simple_facts(files)
    if fact_id in simple:
        return simple[fact_id]
    for resolver in (_settlement_fact, _road_fact, _bridge_fact, _lost_fact, _query_fact):
        found = resolver(files, fact_id)
        if found is not None:
            return found
    return None


def _simple_facts(files: RunFiles) -> dict:
    if getattr(files, "_simple", None) is not None:
        return files._simple
    report = files.report
    track = report.get("track") or {}
    post = track.get("post") or {}
    pre = track.get("pre") or []
    optical = report.get("optical") or {}
    narrow = ((report.get("reliability") or {}).get("narrow_valley") or {})
    out = {
        "run.id": (files.dir.name, "id"),
        "run.event_date": (report.get("event_date"), "date"),
        "run.params_hash": (report.get("parameters_hash"), "id"),
        "run.resolution_m": ((report.get("grid") or {}).get("resolution_m"), "m"),
        "sensor.relative_orbit": (track.get("relative_orbit"), "id"),
        "sensor.direction": (track.get("direction"), "text"),
        "sensor.post_pass_date": (post.get("day"), "date"),
        "sensor.post_latency_days": (track.get("post_latency_days"), "days"),
        "sensor.pre_pass_count": (len(pre), "count"),
        "optical.status": (optical.get("status"), "status"),
        "optical.message": (optical.get("message"), "text"),
        "optical.fused_into_map": ("yes" if optical.get("fused_into_map") else "no", "text"),
        "sensor.sensors_used": ("Sentinel-1 and Sentinel-2"
                                if optical.get("fused_into_map") else "Sentinel-1", "text"),
        "flood.high_ha": (files.area(CLASS_FLOOD_HIGH), "ha"),
        "flood.medium_ha": (files.area(CLASS_FLOOD_MEDIUM), "ha"),
        "flood.permanent_water_ha": (files.area(CLASS_PERMANENT_WATER), "ha"),
        "flood.no_change_ha": (files.area(CLASS_NO_CHANGE), "ha"),
        "flood.not_analysed_ha": (files.area(CLASS_TERRAIN_EXCLUDED), "ha"),
        "flood.extent_ha": (files.extent_ha, "ha"),
        "flood.debris_ha": (files.debris_ha, "ha"),
        "flood.mapped_area_ha": (files.mapped_ha, "ha"),
        "flood.analysed_area_ha": (files.analysed_ha, "ha"),
        "flood.n_polygons": (report.get("n_polygons"), "count"),
        "flood.not_analysed_pct": (_pct(files.area(CLASS_TERRAIN_EXCLUDED),
                                        files.mapped_ha), "percent"),
        "flood.high_share_pct": (_pct(files.area(CLASS_FLOOD_HIGH), files.extent_ha),
                                 "percent"),
        "flood.extent_share_of_analysed_pct": (_pct(files.extent_ha, files.analysed_ha),
                                               "percent"),
        "reliability.narrow_valley_ha": (narrow.get("narrow_valley_ha"), "ha"),
        "reliability.flood_in_narrow_valley_ha":
            (narrow.get("flood_affected_ha_in_narrow_valley"), "ha"),
        "reliability.flood_in_narrow_valley_pct":
            (None if narrow.get("flood_affected_fraction_in_narrow_valley") is None
             else round(100.0 * float(narrow["flood_affected_fraction_in_narrow_valley"]), 4),
             "percent"),
    }
    for name, value in zip(("west", "south", "east", "north"),
                           report.get("bbox_wgs84") or []):
        out[f"run.bbox_{name}"] = (value, "degrees")
    if report.get("bbox_wgs84"):
        out["run.bbox_text"] = (" ".join(str(v) for v in report["bbox_wgs84"]), "text")
    for key, value in (("flood.debris_label", (report.get("flood_extent") or {})
                        .get("debris_label")),
                       ("flood.debris_in_extent",
                        "yes" if (report.get("flood_extent") or {})
                        .get("debris_in_flood_extent") else "no")):
        out[key] = (value, "text")

    if files.has_damage:
        out.update({
            "osm.snapshot_date": (files.damage("osm", "snapshot"), "date"),
            "damage.buildings_total": (files.damage("buildings", "total"), "count"),
            "damage.buildings_damaged": (files.damage("buildings", "damaged"), "count"),
            "damage.buildings_damaged_pct": (_pct(files.damage("buildings", "damaged"),
                                                  files.damage("buildings", "total")),
                                             "percent"),
            "damage.roads_segments_total": (files.damage("roads", "segments_total"),
                                            "count"),
            "damage.roads_segments_cut": (files.damage("roads", "segments_cut"), "count"),
            "damage.roads_length_total_km": (files.damage("roads", "length_total_km"),
                                             "km"),
            "damage.roads_length_cut_km": (files.damage("roads", "length_cut_km"), "km"),
            "damage.roads_length_flooded_km": (files.damage("roads", "length_flooded_km"),
                                               "km"),
            "damage.roads_length_cut_pct": (_pct(files.damage("roads", "length_cut_km"),
                                                 files.damage("roads", "length_total_km")),
                                            "percent"),
            "damage.bridges_total": (files.damage("bridges", "total"), "count"),
            "damage.bridges_likely_out": (files.damage("bridges", "likely_out"), "count"),
            "damage.bridge_status_label": (files.damage("bridges", "status_label"), "text"),
            "damage.caveat": (files.damage("caveat"), "text"),
            "osm.snapshot_rule": (files.damage("osm", "snapshot_rule"), "text"),
        })
    if files.has_cutoff:
        props = files.properties("settlements.geojson")
        out.update({
            "cutoff.settlements_total": (files.cutoff("settlements", "total"), "count"),
            "cutoff.not_attached": (files.cutoff("settlements",
                                                 "not_attached_to_any_graph"), "count"),
            "cutoff.cut_off_count": (files.cutoff("cut_off_by_the_gated_definition"),
                                     "count"),
            "cutoff.probability_runs": (files.cutoff("probability", "runs"), "count"),
            "cutoff.probability_mean": (files.cutoff("probability", "mean"), "probability"),
            "cutoff.at_or_above_half": (files.cutoff("probability", "at_or_above_0.5"),
                                        "count"),
            "cutoff.gated_definition": (f"graph {CUTOFF_GATE_GRAPH}, destination "
                                        f"{CUTOFF_GATE_TARGET}, scenario "
                                        f"{CUTOFF_GATE_SCENARIO}", "text"),
            "cutoff.probability_meaning": (files.cutoff("probability", "what_it_is"),
                                           "text"),
            "cutoff.caveat": (files.cutoff("caveat"), "text"),
        })
        for status, key in ((STATUS_CUT_OFF, "cut_off"), (STATUS_DETOUR, "detour"),
                            (STATUS_NO_BASELINE, "no_route_before_the_flood")):
            out[f"cutoff.count_{key}"] = (
                sum(1 for p in props if p.get("status") == status), "count")
        for target, found in (files.cutoff("destinations") or {}).items():
            out[f"cutoff.destinations_{_slug(target)}"] = (found, "count")
        for kind, graph in (files.cutoff("graphs") or {}).items():
            out[f"cutoff.graph_{_slug(kind)}_length_km"] = (graph.get("length_km"), "km")
    for key, value in (("vocab.sentinel1", "Sentinel-1"), ("vocab.sentinel2", "Sentinel-2"),
                       ("vocab.sentinel1_ne", "सेन्टिनेल-१"),
                       ("vocab.sentinel2_ne", "सेन्टिनेल-२")):
        out[key] = (value, "text")
    out["reliability.caveat"] = (_narrow_caveat() if narrow else None, "text")
    files._simple = out
    return out


def _narrow_caveat() -> str:
    from strandline.config import NARROW_VALLEY_CAVEAT
    return NARROW_VALLEY_CAVEAT


def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", str(text)).strip("_").lower() or "unnamed"


def _settlement_fact(files: RunFiles, fact_id: str):
    match = _SETTLEMENT_RE.match(fact_id)
    if not match:
        return None
    props = files.properties("settlements.geojson")
    index = int(match.group(1))
    if index >= len(props):
        return None
    row, rest = props[index], match.group(2)
    if rest in ("name", "kind"):
        return (row.get(rest), "text")
    if rest == "status":
        return (row.get("status"), "status")
    if rest == "cut_off_probability":
        return (row.get("cut_off_probability"), "probability")
    if rest == "n_buildings":
        return (row.get("n_buildings"), "count")
    return _route_fact(row, rest)


def _route_fact(row: dict, rest: str):
    for graph in GRAPH_KINDS:
        for target in CUTOFF_TARGETS:
            before = row.get(f"{graph}_{target}_before_m")
            if rest == f"{graph}_{target}_before_m":
                return (before, "m")
            if rest == f"{graph}_{target}_before_km":
                return (None if before is None else round(before / 1000.0, 4), "km")
            for scenario in SCENARIOS:
                head = f"{graph}_{target}_{scenario}"
                after = row.get(f"{head}_after_m")
                if rest == f"{head}_status":
                    return (row.get(f"{head}_status"), "status")
                if rest == f"{head}_after_m":
                    return (after, "m")
                if rest == f"{head}_after_km":
                    return (None if after is None else round(after / 1000.0, 4), "km")
                if after is None or before in (None, 0):
                    continue
                if rest == f"{head}_detour_m":
                    return (round(after - before, 4), "m")
                if rest == f"{head}_detour_km":
                    return (round((after - before) / 1000.0, 4), "km")
                if rest == f"{head}_detour_factor":
                    return (round(after / before, 4), "factor")
                if rest == f"{head}_detour_pct":
                    return (round(100.0 * (after - before) / before, 4), "percent")
    return None


def _road_rows(files: RunFiles) -> dict:
    rows = {}
    for i, props in enumerate(files.properties("cut_roads.geojson")):
        key = _slug(props.get("segment_id") or props.get("@osmId") or f"row_{i}")
        rows[key] = props
    return rows


def _road_fact(files: RunFiles, fact_id: str):
    match = _ROAD_RE.match(fact_id)
    if not match:
        return None
    props = _road_rows(files).get(match.group(1))
    if props is None:
        return None
    if match.group(2) == "name":
        return (props.get("name") or props.get("@osmId") or props.get("segment_id"), "text")
    metres = props.get("flooded_length_m")
    if metres is None:
        return None
    if match.group(2) == "flooded_length_m":
        return (metres, "m")
    return (round(float(metres) / 1000.0, 4), "km")


def _bridge_fact(files: RunFiles, fact_id: str):
    match = _BRIDGE_RE.match(fact_id)
    if not match:
        return None
    for i, props in enumerate(files.properties("bridges_out.geojson")):
        identifier = str(props.get("@osmId") or f"bridges_out.geojson#{i}")
        if _slug(identifier) == match.group(1):
            return (props.get("name") or identifier, "text")
    return None


def _lost_fact(files: RunFiles, fact_id: str):
    match = _LOST_RE.match(fact_id)
    if not match:
        return None
    graph, target, scenario, which = match.groups()
    if which == "lost_count":
        return (len(files.lost_route_indices(graph, target, scenario)), "count")
    return (len(files.detour_indices(graph, target, scenario)), "count")


def _query_fact(files: RunFiles, fact_id: str):
    match = _QUERY_RE.match(fact_id)
    if not match:
        return None
    wanted = match.group(1)
    return (sum(1 for name in files.settlement_names() if _slug(name) == wanted), "count")


def text_literals(files: RunFiles) -> set:
    """Names, dates and ids the run contains. Not quantities."""
    out = set()
    for value, unit in _simple_facts(files).values():
        if unit in TEXT_UNITS and value is not None:
            out.add(str(value))
    for props in files.properties("settlements.geojson"):
        for key in ("name", "kind", "status"):
            if props.get(key):
                out.add(str(props[key]))
    for layer, keys in (("cut_roads.geojson", ("name", "@osmId", "segment_id", "highway")),
                        ("bridges_out.geojson", ("name", "@osmId", "highway", "status")),
                        ("damaged_buildings.geojson", ("name", "@osmId"))):
        for props in files.properties(layer):
            for key in keys:
                if props.get(key):
                    out.add(str(props[key]))
    return out
