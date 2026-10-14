"""The evidence store: every number the copilot is allowed to say.

A store is built from ONE run folder - `run_report.json` plus the GeoJSON
layers the pipeline wrote beside it - and nothing else. No reference map, no
published damage product, no number typed by a person and no number written by
a language model ever enters it (Rule 1, Rule 6).

Every fact carries:

    id            a stable dotted name, e.g. "damage.buildings_damaged"
    value, unit   the quantity and what it is measured in
    source_file   the file in the run folder it came out of
    feature_ids   the individual features behind it, where there are any
    inputs        the fact ids it was derived FROM, for a derived number
    derivation    in words, how it was derived

DERIVED NUMBERS ARE FACTS TOO. A percentage, a unit conversion, a sum over a
subset, a count of a filtered list - each is computed here by code and stored
as its own fact BEFORE any answer can use it. That is what lets the verifier
be blunt: a number may appear in an answer if and only if some fact in this
store formats to exactly that string.

WHAT IS MISSING IS ALSO EVIDENCE. If the OSM snapshot could not be read, the
damage layers do not exist and `store.missing["damage"]` says why. The copilot
then says it cannot answer, which is a completely different statement from
"no buildings were damaged" - and is the distinction this module is built
around.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    CUTOFF_TARGETS,
    DEBRIS_UNVERIFIED_LABEL,
    GRAPH_KINDS,
    NARROW_VALLEY_CAVEAT,
    SCENARIOS,
    STATUS_CUT_OFF,
    STATUS_DETOUR,
    STATUS_NO_BASELINE,
)
from copilot.units import format_value

REPORT_FILE = "run_report.json"

# Which GeoJSON layer belongs to which capability, and therefore which
# capability goes missing when the file is not there.
LAYER_FILES = {
    "flood": ("flood_polygons.geojson",),
    "reliability": ("reliability_narrow_valley.geojson",),
    "damage": ("damaged_buildings.geojson", "cut_roads.geojson", "bridges_out.geojson"),
    "cutoff": ("settlements.geojson", "routes.geojson"),
}

# run_report.json's `damage.status` / `cutoff.status` value that means there IS
# an answer. Anything else is a refusal with a recorded reason.
STATUS_USED = "used"


class EvidenceError(RuntimeError):
    """The run folder cannot be read at all (missing, or not a run folder)."""


class MissingEvidence(LookupError):
    """A tool asked for a capability this run does not have.

    Carries the reason the pipeline recorded, so the copilot can say WHY it
    cannot answer rather than answering zero.
    """

    def __init__(self, capability: str, reason: str):
        super().__init__(reason)
        self.capability = capability
        self.reason = reason


@dataclass(frozen=True)
class Fact:
    """One number (or one piece of text) the copilot may put in an answer."""

    id: str
    value: object
    unit: str | None
    source_file: str
    feature_ids: tuple = ()
    inputs: tuple = ()
    derivation: str | None = None

    def formatted(self, lang: str = "en") -> str:
        return format_value(self.value, self.unit, lang)

    def as_dict(self, lang: str = "en") -> dict:
        return {
            "fact_id": self.id,
            "value": self.value,
            "formatted": self.formatted(lang),
            "unit": self.unit,
            "source_file": self.source_file,
            "feature_ids": list(self.feature_ids),
            "inputs": list(self.inputs),
            "derivation": self.derivation,
        }


def slug(text: str) -> str:
    """A stable key fragment for a fact id."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", str(text)).strip("_").lower()
    return cleaned or "unnamed"


def _num(value):
    """A finite number, or None. Never a NaN, never a bool dressed as one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return value if number == number and number not in (float("inf"), float("-inf")) \
            else None
    return None


@dataclass
class EvidenceStore:
    """Facts, layers, and an honest account of what is not here."""

    run_dir: Path
    report: dict
    facts: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)
    missing: dict = field(default_factory=dict)

    # --- building ---------------------------------------------------------

    @classmethod
    def from_run(cls, run_dir) -> "EvidenceStore":
        run_dir = Path(run_dir)
        path = run_dir / REPORT_FILE
        if not path.exists():
            raise EvidenceError(
                f"{run_dir} is not a Strandline run folder: no {REPORT_FILE}. "
                f"Produce one first:\n"
                f"  venv/Scripts/python.exe -m strandline.run --bbox W S E N "
                f"--event-date YYYY-MM-DD --out {run_dir}")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise EvidenceError(f"{path} is not readable JSON: {err}") from err
        store = cls(run_dir=run_dir, report=report)
        store._load_layers()
        store._build()
        return store

    def _load_layers(self) -> None:
        for capability, files in LAYER_FILES.items():
            for name in files:
                path = self.run_dir / name
                if not path.exists():
                    continue
                try:
                    self.layers[name] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as err:
                    self.missing.setdefault(
                        capability, f"{name} is present but unreadable: {err}")

    # --- recording --------------------------------------------------------

    def add(self, fact: Fact) -> Fact:
        """Record a fact. Re-recording an id with a different value is a bug
        in a tool, not something to paper over, so it raises."""
        existing = self.facts.get(fact.id)
        if existing is not None and existing.value != fact.value:
            raise EvidenceError(
                f"fact {fact.id!r} already recorded as {existing.value!r}, "
                f"now {fact.value!r}: two derivations share one id")
        self.facts[fact.id] = fact
        return fact

    def derive(self, fact_id: str, value, unit: str | None, *, inputs,
               derivation: str, source_file: str = REPORT_FILE,
               feature_ids=()) -> Fact:
        """Record a number computed FROM other facts. The only way a
        percentage, a sum or a conversion may reach an answer."""
        return self.add(Fact(id=fact_id, value=value, unit=unit, source_file=source_file,
                             feature_ids=tuple(feature_ids), inputs=tuple(inputs),
                             derivation=derivation))

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def need(self, capability: str) -> None:
        """Raise MissingEvidence unless this run has `capability`."""
        if capability in self.missing:
            raise MissingEvidence(capability, self.missing[capability])

    def features(self, layer: str) -> list:
        return list((self.layers.get(layer) or {}).get("features") or [])

    def text_literals(self) -> set:
        """Only the NON-quantity literals: names, ids, dates, mission names.

        This is what a TEMPLATE may contain, and it is deliberately much
        narrower than `allowed_strings`. A template that happens to contain
        "12" would otherwise pass whenever some unrelated fact in the run is
        also 12 - and the model is not supposed to write a quantity at all.
        """
        out = set()
        for fact in self.facts.values():
            if fact.unit in ("date", "text", "id", "class", "status"):
                out.add(str(fact.value))
        out.discard("None")
        return out

    def allowed_strings(self, lang: str = "en") -> set:
        """Every literal a verified answer may contain, formatted for `lang`."""
        out = set()
        for fact in self.facts.values():
            out.add(fact.formatted(lang))
            if fact.unit in ("date", "id", "text", "class", "status"):
                out.add(str(fact.value))
        out.discard("—")
        out.discard("None")
        return out

    # --- fact construction ------------------------------------------------

    def _report_fact(self, fact_id, value, unit, *, keys, feature_ids=()) -> Fact | None:
        """A fact read straight out of run_report.json. A None value is NOT
        recorded: an absent measurement must never become a zero."""
        if value is None:
            return None
        return self.add(Fact(id=fact_id, value=value, unit=unit, source_file=REPORT_FILE,
                             feature_ids=tuple(feature_ids),
                             derivation=f"run_report.json: {keys}"))

    def _build(self) -> None:
        self._build_vocabulary()
        self._build_run()
        self._build_sensor()
        self._build_flood()
        self._build_reliability()
        self._build_damage()
        self._build_cutoff()

    def _build_vocabulary(self) -> None:
        """Product names that happen to contain a digit.

        "Sentinel-1" is not a quantity, but its "1" is a digit, and the
        verifier only knows what the evidence declares. Declaring the names
        here - in both scripts, since a Nepali answer writes them in
        Devanagari - keeps the verifier strict about everything else instead
        of forcing an exception into it.
        """
        for key, value in (("sentinel1", "Sentinel-1"), ("sentinel2", "Sentinel-2"),
                           ("sentinel1_ne", "सेन्टिनेल-१"), ("sentinel2_ne", "सेन्टिनेल-२")):
            self.add(Fact(id=f"vocab.{key}", value=value, unit="text",
                          source_file=REPORT_FILE,
                          derivation="the name of an input mission, declared so the "
                                     "verifier does not read its digit as a quantity"))

    def _build_run(self) -> None:
        r = self.report
        self._report_fact("run.id", self.run_dir.name, "id", keys="(run folder name)")
        self._report_fact("run.event_date", r.get("event_date"), "date", keys="event_date")
        self._report_fact("run.params_hash", r.get("parameters_hash"), "id",
                          keys="parameters_hash")
        grid = r.get("grid") or {}
        self._report_fact("run.resolution_m", _num(grid.get("resolution_m")), "m",
                          keys="grid.resolution_m")
        bbox = r.get("bbox_wgs84") or []
        for name, value in zip(("west", "south", "east", "north"), bbox):
            self._report_fact(f"run.bbox_{name}", _num(value), "degrees",
                              keys=f"bbox_wgs84[{name}]")
        if len(bbox) == 4 and all(_num(v) is not None for v in bbox):
            self.derive("run.bbox_text",
                        " ".join(str(v) for v in bbox), "text",
                        inputs=("run.bbox_west", "run.bbox_south",
                                "run.bbox_east", "run.bbox_north"),
                        derivation="the four bbox corners, written W S E N")

    def _build_sensor(self) -> None:
        track = self.report.get("track") or {}
        post = track.get("post") or {}
        self._report_fact("sensor.relative_orbit", track.get("relative_orbit"), "id",
                          keys="track.relative_orbit")
        self._report_fact("sensor.direction", track.get("direction"), "text",
                          keys="track.direction")
        self._report_fact("sensor.post_pass_date", post.get("day"), "date",
                          keys="track.post.day",
                          feature_ids=tuple(post.get("scenes") or ()))
        self._report_fact("sensor.post_latency_days", _num(track.get("post_latency_days")),
                          "days", keys="track.post_latency_days")
        pre = track.get("pre") or []
        self._report_fact("sensor.pre_pass_count", len(pre), "count", keys="len(track.pre)")
        if pre:
            self._report_fact("sensor.pre_first_date", pre[0].get("day"), "date",
                              keys="track.pre[0].day")
            self._report_fact("sensor.pre_last_date", pre[-1].get("day"), "date",
                              keys="track.pre[-1].day")
        optical = self.report.get("optical") or {}
        self._report_fact("optical.status", optical.get("status"), "status",
                          keys="optical.status")
        self._report_fact("optical.message", optical.get("message"), "text",
                          keys="optical.message")
        fused = bool(optical.get("fused_into_map"))
        self._report_fact("optical.fused_into_map", "yes" if fused else "no", "text",
                          keys="optical.fused_into_map")
        # No trailing "only": this is a name, and it is dropped verbatim into
        # a Nepali sentence as well as an English one.
        self._report_fact("sensor.sensors_used",
                          "Sentinel-1 and Sentinel-2" if fused else "Sentinel-1",
                          "text", keys="optical.fused_into_map")

    def _build_flood(self) -> None:
        areas = self.report.get("areas_ha") or {}
        extent = self.report.get("flood_extent") or {}
        by_class = {name: _num(value) for name, value in areas.items()}

        def area(code):
            return by_class.get(CLASS_NAMES[code]) or 0.0

        self._report_fact("flood.high_ha", area(CLASS_FLOOD_HIGH), "ha",
                          keys=f"areas_ha[{CLASS_NAMES[CLASS_FLOOD_HIGH]!r}]")
        self._report_fact("flood.medium_ha", area(CLASS_FLOOD_MEDIUM), "ha",
                          keys=f"areas_ha[{CLASS_NAMES[CLASS_FLOOD_MEDIUM]!r}]")
        self._report_fact("flood.permanent_water_ha", area(CLASS_PERMANENT_WATER), "ha",
                          keys=f"areas_ha[{CLASS_NAMES[CLASS_PERMANENT_WATER]!r}]")
        self._report_fact("flood.no_change_ha", area(CLASS_NO_CHANGE), "ha",
                          keys=f"areas_ha[{CLASS_NAMES[CLASS_NO_CHANGE]!r}]")
        self._report_fact("flood.not_analysed_ha", area(CLASS_TERRAIN_EXCLUDED), "ha",
                          keys=f"areas_ha[{CLASS_NAMES[CLASS_TERRAIN_EXCLUDED]!r}]")

        # Flood EXTENT is the pipeline's OWN declared quantity, not a sum
        # invented here: ExtentParams decides whether debris counts, and the
        # report states the answer. Only a report that predates that block
        # gets the sum, and then it is labelled as derived.
        extent_ha = _num(extent.get("flood_extent_ha"))
        if extent_ha is not None:
            self._report_fact("flood.extent_ha", extent_ha, "ha",
                              keys="flood_extent.flood_extent_ha")
        else:
            self.derive("flood.extent_ha",
                        round(area(CLASS_FLOOD_HIGH) + area(CLASS_FLOOD_MEDIUM), 3), "ha",
                        inputs=("flood.high_ha", "flood.medium_ha"),
                        derivation="high- plus medium-confidence flood water")
        debris_ha = _num(extent.get("debris_ha"))
        self._report_fact("flood.debris_ha",
                          area(CLASS_DEBRIS) if debris_ha is None else debris_ha, "ha",
                          keys="flood_extent.debris_ha")
        self._report_fact("flood.debris_label",
                          extent.get("debris_label") or DEBRIS_UNVERIFIED_LABEL, "text",
                          keys="flood_extent.debris_label")
        self._report_fact("flood.debris_in_extent",
                          "yes" if extent.get("debris_in_flood_extent") else "no", "text",
                          keys="flood_extent.debris_in_flood_extent")
        self._report_fact("flood.n_polygons", _num(self.report.get("n_polygons")), "count",
                          keys="n_polygons")

        total = round(sum(v for v in by_class.values() if v is not None), 3)
        self.derive("flood.mapped_area_ha", total, "ha",
                    inputs=tuple(f"flood.{k}_ha" for k in
                                 ("high", "medium", "permanent_water", "no_change",
                                  "not_analysed") if self.get(f"flood.{k}_ha")),
                    derivation="sum of every class area in areas_ha")
        analysed = round(total - area(CLASS_TERRAIN_EXCLUDED), 3)
        self.derive("flood.analysed_area_ha", analysed, "ha",
                    inputs=("flood.mapped_area_ha", "flood.not_analysed_ha"),
                    derivation="mapped area minus the not-analysed (steep or high) class")
        self._percent("flood.not_analysed_pct", area(CLASS_TERRAIN_EXCLUDED), total,
                      "flood.not_analysed_ha", "flood.mapped_area_ha",
                      "not-analysed area as a share of the mapped area")
        ext = self.get("flood.extent_ha")
        if ext is not None:
            self._percent("flood.high_share_pct", area(CLASS_FLOOD_HIGH),
                          _num(ext.value) or 0.0, "flood.high_ha", "flood.extent_ha",
                          "high-confidence water as a share of the flood extent")
            self._percent("flood.extent_share_of_analysed_pct", _num(ext.value) or 0.0,
                          analysed, "flood.extent_ha", "flood.analysed_area_ha",
                          "flood extent as a share of the analysed area")

    def _percent(self, fact_id, numerator, denominator, num_id, den_id, words) -> None:
        """A share, recorded as its own fact.

        A zero or absent denominator records the fact with value None, not
        with a zero and not by leaving it out: "no buildings are mapped here,
        so the share is undefined" is a real answer, and it formats as an em
        dash that the verifier does not treat as a number.
        """
        if numerator is None or not denominator:
            self.derive(fact_id, None, "percent", inputs=(num_id, den_id),
                        derivation=f"100 x {num_id} / {den_id}: {words} - undefined, "
                                   f"the denominator is zero or absent")
            return
        self.derive(fact_id, round(100.0 * float(numerator) / float(denominator), 4),
                    "percent", inputs=(num_id, den_id),
                    derivation=f"100 x {num_id} / {den_id}: {words}")

    def _build_reliability(self) -> None:
        block = ((self.report.get("reliability") or {}).get("narrow_valley") or {})
        if not block:
            self.missing.setdefault(
                "reliability", "this run recorded no narrow-valley reliability layer")
            return
        self._report_fact("reliability.narrow_valley_ha", _num(block.get("narrow_valley_ha")),
                          "ha", keys="reliability.narrow_valley.narrow_valley_ha")
        self._report_fact("reliability.flood_in_narrow_valley_ha",
                          _num(block.get("flood_affected_ha_in_narrow_valley")), "ha",
                          keys="reliability.narrow_valley."
                               "flood_affected_ha_in_narrow_valley")
        fraction = _num(block.get("flood_affected_fraction_in_narrow_valley"))
        if fraction is not None:
            self.derive("reliability.flood_in_narrow_valley_pct", round(100.0 * fraction, 4),
                        "percent", inputs=("reliability.flood_in_narrow_valley_ha",),
                        derivation="100 x flood_affected_fraction_in_narrow_valley")
        self._report_fact("reliability.caveat", NARROW_VALLEY_CAVEAT, "text",
                          keys="strandline.config.NARROW_VALLEY_CAVEAT")

    # --- damage -----------------------------------------------------------

    def _build_damage(self) -> None:
        block = self.report.get("damage")
        if not block:
            self.missing["damage"] = (
                "this run has no damage section in run_report.json: it was produced "
                "before the damage overlay existed, or without it")
            return
        if block.get("status") != STATUS_USED:
            self.missing["damage"] = (
                f"the damage overlay did not run ({block.get('status')}): "
                f"{block.get('message') or 'no reason recorded'}")
            return
        for name in LAYER_FILES["damage"]:
            if name not in self.layers:
                self.missing["damage"] = (
                    f"run_report.json says the damage overlay ran, but {name} is not in "
                    f"{self.run_dir}: the run folder is incomplete")
                return

        osm = block.get("osm") or {}
        self._report_fact("osm.snapshot_date", osm.get("snapshot"), "date",
                          keys="damage.osm.snapshot")
        self._report_fact("osm.snapshot_rule", osm.get("snapshot_rule"), "text",
                          keys="damage.osm.snapshot_rule")
        buildings = block.get("buildings") or {}
        roads = block.get("roads") or {}
        bridges = block.get("bridges") or {}

        damaged_ids = self._feature_ids("damaged_buildings.geojson")
        self._report_fact("damage.buildings_total", _num(buildings.get("total")), "count",
                          keys="damage.buildings.total")
        self._report_fact("damage.buildings_damaged", _num(buildings.get("damaged")), "count",
                          keys="damage.buildings.damaged", feature_ids=damaged_ids)
        self._percent("damage.buildings_damaged_pct", _num(buildings.get("damaged")),
                      _num(buildings.get("total")), "damage.buildings_damaged",
                      "damage.buildings_total", "damaged buildings as a share of all mapped")

        cut_ids = self._feature_ids("cut_roads.geojson")
        self._report_fact("damage.roads_segments_total", _num(roads.get("segments_total")),
                          "count", keys="damage.roads.segments_total")
        self._report_fact("damage.roads_segments_cut", _num(roads.get("segments_cut")),
                          "count", keys="damage.roads.segments_cut", feature_ids=cut_ids)
        self._report_fact("damage.roads_length_total_km", _num(roads.get("length_total_km")),
                          "km", keys="damage.roads.length_total_km")
        self._report_fact("damage.roads_length_cut_km", _num(roads.get("length_cut_km")),
                          "km", keys="damage.roads.length_cut_km", feature_ids=cut_ids)
        self._report_fact("damage.roads_length_flooded_km",
                          _num(roads.get("length_flooded_km")), "km",
                          keys="damage.roads.length_flooded_km", feature_ids=cut_ids)
        self._percent("damage.roads_length_cut_pct", _num(roads.get("length_cut_km")),
                      _num(roads.get("length_total_km")), "damage.roads_length_cut_km",
                      "damage.roads_length_total_km", "cut road length as a share of all")

        bridge_ids = self._feature_ids("bridges_out.geojson")
        self._report_fact("damage.bridges_total", _num(bridges.get("total")), "count",
                          keys="damage.bridges.total")
        self._report_fact("damage.bridges_likely_out", _num(bridges.get("likely_out")),
                          "count", keys="damage.bridges.likely_out", feature_ids=bridge_ids)
        self._report_fact("damage.bridge_status_label",
                          bridges.get("status_label") or BRIDGE_UNVERIFIED_LABEL, "text",
                          keys="damage.bridges.status_label")
        self._report_fact("damage.caveat", block.get("caveat"), "text", keys="damage.caveat")

    def _feature_ids(self, layer: str) -> tuple:
        """One id per feature, in layer order.

        `segment_id` comes FIRST where a layer has it: one OSM way is split
        into several road segments, so listing `@osmId` would repeat an id and
        a reader counting the list would not get the count the fact states.
        """
        out = []
        for i, feature in enumerate(self.features(layer)):
            props = feature.get("properties") or {}
            identifier = props.get("segment_id") or props.get("@osmId")
            out.append(str(identifier) if identifier else f"{layer}#{i}")
        return tuple(out)

    # --- cut-off ----------------------------------------------------------

    def _build_cutoff(self) -> None:
        block = self.report.get("cutoff")
        if not block:
            self.missing["cutoff"] = (
                "this run has no cut-off section in run_report.json: it was produced "
                "before the cut-off analysis existed, or without it")
            return
        if block.get("status") != STATUS_USED:
            self.missing["cutoff"] = (
                f"the cut-off analysis did not run ({block.get('status')}): "
                f"{block.get('message') or 'no reason recorded'}")
            return
        if "settlements.geojson" not in self.layers:
            self.missing["cutoff"] = (
                f"run_report.json says the cut-off analysis ran, but settlements.geojson "
                f"is not in {self.run_dir}: the run folder is incomplete")
            return

        settlements = block.get("settlements") or {}
        probability = block.get("probability") or {}
        self._report_fact("cutoff.settlements_total", _num(settlements.get("total")),
                          "count", keys="cutoff.settlements.total")
        self._report_fact("cutoff.not_attached",
                          _num(settlements.get("not_attached_to_any_graph")), "count",
                          keys="cutoff.settlements.not_attached_to_any_graph")
        self._report_fact("cutoff.cut_off_count",
                          _num(block.get("cut_off_by_the_gated_definition")), "count",
                          keys="cutoff.cut_off_by_the_gated_definition")
        self._report_fact("cutoff.probability_runs", _num(probability.get("runs")), "count",
                          keys="cutoff.probability.runs")
        self._report_fact("cutoff.probability_mean", _num(probability.get("mean")),
                          "probability", keys="cutoff.probability.mean")
        self._report_fact("cutoff.at_or_above_half",
                          _num(probability.get("at_or_above_0.5")), "count",
                          keys="cutoff.probability['at_or_above_0.5']")
        self._report_fact("cutoff.gated_definition",
                          f"graph {CUTOFF_GATE_GRAPH}, destination {CUTOFF_GATE_TARGET}, "
                          f"scenario {CUTOFF_GATE_SCENARIO}", "text",
                          keys="cutoff.definition.gated_definition")
        self._report_fact("cutoff.probability_meaning", probability.get("what_it_is"),
                          "text", keys="cutoff.probability.what_it_is")
        self._report_fact("cutoff.caveat", block.get("caveat"), "text", keys="cutoff.caveat")
        for target, found in sorted((block.get("destinations") or {}).items()):
            self._report_fact(f"cutoff.destinations_{slug(target)}", _num(found), "count",
                              keys=f"cutoff.destinations.{target}")
        for kind, graph in sorted((block.get("graphs") or {}).items()):
            self._report_fact(f"cutoff.graph_{slug(kind)}_length_km",
                              _num(graph.get("length_km")), "km",
                              keys=f"cutoff.graphs.{kind}.length_km")

        self._build_settlement_facts()

    def _build_settlement_facts(self) -> None:
        """One block of facts per settlement, keyed by POSITION in the layer.

        Position, not name: `network.py` manufactures duplicate names (every
        untagged hamlet becomes "unnamed hamlet"), so a name is not an
        identity. The name is itself a fact, so an answer can still say it.
        """
        counts = {STATUS_CUT_OFF: 0, STATUS_DETOUR: 0, STATUS_NO_BASELINE: 0}
        features = self.features("settlements.geojson")
        for i, feature in enumerate(features):
            props = feature.get("properties") or {}
            base = f"settlement.{i}"
            fid = f"settlements.geojson#{i}"
            self.add(Fact(id=f"{base}.name", value=props.get("name"), unit="text",
                          source_file="settlements.geojson", feature_ids=(fid,),
                          derivation="settlements.geojson properties.name"))
            self.add(Fact(id=f"{base}.kind", value=props.get("kind"), unit="text",
                          source_file="settlements.geojson", feature_ids=(fid,),
                          derivation="settlements.geojson properties.kind"))
            self.add(Fact(id=f"{base}.status", value=props.get("status"), unit="status",
                          source_file="settlements.geojson", feature_ids=(fid,),
                          derivation="settlements.geojson properties.status "
                                     "(the gated definition)"))
            if props.get("status") in counts:
                counts[props["status"]] += 1
            probability = _num(props.get("cut_off_probability"))
            if probability is not None:
                self.add(Fact(id=f"{base}.cut_off_probability", value=probability,
                              unit="probability", source_file="settlements.geojson",
                              feature_ids=(fid,),
                              derivation="settlements.geojson properties."
                                         "cut_off_probability (ensemble share)"))
            buildings = _num(props.get("n_buildings"))
            if buildings is not None:
                self.add(Fact(id=f"{base}.n_buildings", value=buildings, unit="count",
                              source_file="settlements.geojson", feature_ids=(fid,),
                              derivation="settlements.geojson properties.n_buildings"))
            for kind in GRAPH_KINDS:
                for target in CUTOFF_TARGETS:
                    self._route_facts(base, fid, props, kind, target)
        status_inputs = tuple(f"settlement.{i}.status" for i in range(len(features)))
        for status, number in counts.items():
            self.derive(f"cutoff.count_{slug(status)}", number, "count",
                        inputs=status_inputs,
                        derivation=f"settlements whose gated status is {status!r}",
                        source_file="settlements.geojson")

    def _route_facts(self, base: str, fid: str, props: dict, kind: str, target: str) -> None:
        before = _num(props.get(f"{kind}_{target}_before_m"))
        if before is not None:
            self.add(Fact(id=f"{base}.{kind}_{target}_before_m", value=before, unit="m",
                          source_file="settlements.geojson", feature_ids=(fid,),
                          derivation=f"shortest {kind} route to the nearest {target} "
                                     f"before the flood"))
            self.derive(f"{base}.{kind}_{target}_before_km", round(before / 1000.0, 4), "km",
                        inputs=(f"{base}.{kind}_{target}_before_m",),
                        derivation="metres / 1000", source_file="settlements.geojson",
                        feature_ids=(fid,))
        for scenario in SCENARIOS:
            after = _num(props.get(f"{kind}_{target}_{scenario}_after_m"))
            status = props.get(f"{kind}_{target}_{scenario}_status")
            key = f"{base}.{kind}_{target}_{scenario}"
            if status is not None:
                self.add(Fact(id=f"{key}_status", value=status, unit="status",
                              source_file="settlements.geojson", feature_ids=(fid,),
                              derivation=f"reachability of the nearest {target} by "
                                         f"{kind}, {scenario}"))
            if after is None:
                continue
            self.add(Fact(id=f"{key}_after_m", value=after, unit="m",
                          source_file="settlements.geojson", feature_ids=(fid,),
                          derivation=f"shortest {kind} route to the nearest {target} "
                                     f"after the flood, {scenario}"))
            self.derive(f"{key}_after_km", round(after / 1000.0, 4), "km",
                        inputs=(f"{key}_after_m",), derivation="metres / 1000",
                        source_file="settlements.geojson", feature_ids=(fid,))
            if before is None or before <= 0:
                continue
            self.derive(f"{key}_detour_m", round(after - before, 4), "m",
                        inputs=(f"{key}_after_m", f"{base}.{kind}_{target}_before_m"),
                        derivation="route after the flood minus route before it",
                        source_file="settlements.geojson", feature_ids=(fid,))
            self.derive(f"{key}_detour_km", round((after - before) / 1000.0, 4), "km",
                        inputs=(f"{key}_detour_m",), derivation="metres / 1000",
                        source_file="settlements.geojson", feature_ids=(fid,))
            self.derive(f"{key}_detour_factor", round(after / before, 4), "factor",
                        inputs=(f"{key}_after_m", f"{base}.{kind}_{target}_before_m"),
                        derivation="route after the flood divided by route before it",
                        source_file="settlements.geojson", feature_ids=(fid,))
            self.derive(f"{key}_detour_pct", round(100.0 * (after - before) / before, 4),
                        "percent", inputs=(f"{key}_detour_m",
                                           f"{base}.{kind}_{target}_before_m"),
                        derivation="100 x detour / route before the flood",
                        source_file="settlements.geojson", feature_ids=(fid,))

    # --- convenience ------------------------------------------------------

    def settlement_count(self) -> int:
        return len(self.features("settlements.geojson"))

    def settlement_name(self, index: int) -> str:
        fact = self.get(f"settlement.{index}.name")
        return str(fact.value) if fact is not None else f"settlement {index}"

    def find_settlements(self, query: str) -> list:
        """Indices of settlements whose name matches `query`, case-folded.

        Exact matches first; only if there are none, substring matches. A LIST
        because OSM names are not unique, and pretending otherwise would
        silently answer about a different village of the same name.
        """
        wanted = slug(query)
        if not wanted:
            return []
        exact, partial = [], []
        for i in range(self.settlement_count()):
            name = slug(self.settlement_name(i))
            if name == wanted:
                exact.append(i)
            elif len(wanted) >= 3 and (wanted in name or name in wanted):
                partial.append(i)
        return exact or partial
