"""The tools. One per question the copilot can answer from a run folder.

A tool reads the evidence store, records any number it had to derive as a new
fact, and returns the SLOTS a template may use - never a sentence. The wording
comes from `copilot/templates.py`; the numbers come from here; the two meet
only in the filler, which can put nothing in a slot that is not a fact.

Every tool declares the evidence capability it needs. If the run does not have
it - OSM refused, the cut-off analysis did not run - the tool raises
`MissingEvidence` with the reason the pipeline recorded, and the copilot says
so. It never answers zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strandline.config import (
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    CUTOFF_TARGETS,
    GRAPH_KINDS,
    SCENARIOS,
    STATUS_CUT_OFF,
    STATUS_DETOUR,
)
from copilot.dsl import Parameter, ToolSpec
from copilot.evidence import EvidenceStore, Fact, MissingEvidence, slug

MAX_LIST = 20


@dataclass(frozen=True)
class Slot:
    """One placeholder a template may use, already written out for a language.

    `fact` is the single fact behind a plain number. `facts` is everything
    behind a list slot, which code - never the model - assembled from facts.
    """

    name: str
    text: str
    fact: Fact | None = None
    facts: tuple = ()

    def all_facts(self) -> tuple:
        return (self.fact,) + tuple(self.facts) if self.fact is not None else tuple(self.facts)


@dataclass
class ToolResult:
    tool: str
    template_key: str
    slots: dict = field(default_factory=dict)
    caveat_fact_ids: tuple = ()
    note: str | None = None

    def facts(self) -> list:
        """Every fact behind this result, de-duplicated, in a stable order."""
        out, seen = [], set()
        for slot in self.slots.values():
            for fact in slot.all_facts():
                if fact.id not in seen:
                    seen.add(fact.id)
                    out.append(fact)
        return out

    def feature_ids(self) -> tuple:
        out, seen = [], set()
        for fact in self.facts():
            for fid in fact.feature_ids:
                if fid not in seen:
                    seen.add(fid)
                    out.append(fid)
        return tuple(out)

    def source_files(self) -> tuple:
        return tuple(sorted({f.source_file for f in self.facts()}))


# --- helpers ----------------------------------------------------------------


def _slot(store: EvidenceStore, fact_id: str, name: str, lang: str,
          capability: str = "flood", required: bool = True, i18n=None) -> Slot:
    """One placeholder, from one fact.

    `required=False` is for a fact a run may legitimately not have - the OSM
    snapshot date when OSM was never read. The slot then says so in words
    rather than showing a blank, because a reader must not have to guess
    whether a gap is a zero.
    """
    fact = store.get(fact_id)
    if fact is None:
        if required:
            raise MissingEvidence(
                capability,
                f"this run records no {fact_id!r}, so the question cannot be "
                f"answered from its evidence")
        return Slot(name=name,
                    text=i18n["labels"]["not_recorded"] if i18n else "—")
    return Slot(name=name, text=fact.formatted(lang), fact=fact)


def _text_slot(name: str, text: str, facts=()) -> Slot:
    return Slot(name=name, text=text, facts=tuple(facts))


def _join(items, i18n) -> str:
    separator = i18n["labels"]["list_separator"]
    return separator.join(items) if items else i18n["labels"]["none"]


def _feature_name(props: dict, i18n, fallback_key: str = "unnamed") -> str:
    for key in ("name", "@osmId", "segment_id"):
        value = props.get(key)
        if value not in (None, "", "nan"):
            return str(value)
    return i18n["labels"][fallback_key]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


# --- flood ------------------------------------------------------------------


def flooded_area(store: EvidenceStore, lang: str, i18n, *, by_confidence: bool = True
                 ) -> ToolResult:
    names = ["extent_ha", "high_ha", "medium_ha", "high_share_pct", "permanent_water_ha",
             "analysed_area_ha", "extent_share_of_analysed_pct"]
    slots = {name: _slot(store, f"flood.{name}", name, lang) for name in names}
    return ToolResult(tool="flooded_area", template_key="flooded_area", slots=slots,
                      caveat_fact_ids=("reliability.caveat",))


def debris(store: EvidenceStore, lang: str, i18n) -> ToolResult:
    slots = {name: _slot(store, f"flood.{name}", name, lang)
             for name in ("debris_ha", "debris_label", "debris_in_extent")}
    return ToolResult(tool="debris", template_key="debris", slots=slots)


# --- damage -----------------------------------------------------------------


def damaged_buildings(store: EvidenceStore, lang: str, i18n) -> ToolResult:
    store.need("damage")
    slots = {name: _slot(store, f"damage.{name}", name, lang, "damage")
             for name in ("buildings_damaged", "buildings_total")}
    slots["buildings_damaged_pct"] = _slot(store, "damage.buildings_damaged_pct",
                                           "buildings_damaged_pct", lang, "damage",
                                           required=False, i18n=i18n)
    return ToolResult(tool="damaged_buildings", template_key="damaged_buildings",
                      slots=slots, caveat_fact_ids=("damage.caveat", "osm.snapshot_date"))


def cut_roads(store: EvidenceStore, lang: str, i18n, *, top: int = 5) -> ToolResult:
    store.need("damage")
    slots = {name: _slot(store, f"damage.{name}", name, lang, "damage")
             for name in ("roads_segments_cut", "roads_segments_total",
                          "roads_length_cut_km", "roads_length_total_km",
                          "roads_length_flooded_km")}
    rows = []
    for i, feature in enumerate(store.features("cut_roads.geojson")):
        props = feature.get("properties") or {}
        flooded_m = _number(props.get("flooded_length_m"))
        if flooded_m is None:
            continue
        key = slug(props.get("segment_id") or props.get("@osmId") or f"row_{i}")
        fid = str(props.get("segment_id") or props.get("@osmId") or f"cut_roads.geojson#{i}")
        name_fact = store.add(Fact(id=f"road.{key}.name", value=_feature_name(props, i18n),
                                   unit="text", source_file="cut_roads.geojson",
                                   feature_ids=(fid,),
                                   derivation="cut_roads.geojson properties.name "
                                              "(or the OSM id, when it has no name)"))
        length_fact = store.add(Fact(id=f"road.{key}.flooded_length_m", value=flooded_m,
                                     unit="m", source_file="cut_roads.geojson",
                                     feature_ids=(fid,),
                                     derivation="cut_roads.geojson "
                                                "properties.flooded_length_m"))
        km_fact = store.derive(f"road.{key}.flooded_length_km", round(flooded_m / 1000.0, 4),
                               "km", inputs=(length_fact.id,), derivation="metres / 1000",
                               source_file="cut_roads.geojson", feature_ids=(fid,))
        rows.append((flooded_m, name_fact, km_fact))
    rows.sort(key=lambda row: (-row[0], row[1].id))
    rows = rows[:max(0, min(int(top), MAX_LIST))]
    items = [i18n["labels"]["road_item"].format(name=name.formatted(lang),
                                                flooded_km=km.formatted(lang))
             for _, name, km in rows]
    facts = [f for _, name, km in rows for f in (name, km)]
    slots["road_list"] = _text_slot("road_list", _join(items, i18n), facts)
    return ToolResult(tool="cut_roads", template_key="cut_roads", slots=slots,
                      caveat_fact_ids=("damage.caveat", "osm.snapshot_date"))


def bridges_out(store: EvidenceStore, lang: str, i18n, *, top: int = 5) -> ToolResult:
    store.need("damage")
    slots = {name: _slot(store, f"damage.{name}", name, lang, "damage")
             for name in ("bridges_likely_out", "bridges_total", "bridge_status_label")}
    facts, items = [], []
    for i, feature in enumerate(store.features("bridges_out.geojson")[:MAX_LIST]):
        props = feature.get("properties") or {}
        fid = str(props.get("@osmId") or f"bridges_out.geojson#{i}")
        key = slug(fid)
        fact = store.add(Fact(id=f"bridge.{key}.name", value=_feature_name(props, i18n),
                              unit="text", source_file="bridges_out.geojson",
                              feature_ids=(fid,),
                              derivation="bridges_out.geojson properties.name "
                                         "(or the OSM id, when it has no name)"))
        facts.append(fact)
        items.append(i18n["labels"]["bridge_item"].format(name=fact.formatted(lang)))
    items = items[:max(0, min(int(top), MAX_LIST))]
    slots["bridge_list"] = _text_slot("bridge_list", _join(items, i18n), facts[:len(items)])
    return ToolResult(tool="bridges_out", template_key="bridges_out", slots=slots,
                      caveat_fact_ids=("damage.caveat", "osm.snapshot_date"))


# --- cut-off ----------------------------------------------------------------


def _settlement_rows(store: EvidenceStore):
    """(index, status, probability) for every settlement, in layer order."""
    for i in range(store.settlement_count()):
        status = store.get(f"settlement.{i}.status")
        probability = store.get(f"settlement.{i}.cut_off_probability")
        yield i, (status.value if status else None), (
            probability.value if probability else None)


def cutoff_settlements(store: EvidenceStore, lang: str, i18n, *, top: int = 5,
                       min_probability: float = 0.0) -> ToolResult:
    store.need("cutoff")
    slots = {name: _slot(store, f"cutoff.{name}", name, lang, "cutoff")
             for name in ("cut_off_count", "settlements_total", "at_or_above_half",
                          "probability_runs", "gated_definition")}
    rows = [(i, p if p is not None else 0.0) for i, status, p in _settlement_rows(store)
            if status == STATUS_CUT_OFF and (p is None or p >= min_probability)]
    rows.sort(key=lambda row: (-row[1], row[0]))
    items, facts = [], []
    for i, _ in rows[:max(0, min(int(top), MAX_LIST))]:
        name = store.get(f"settlement.{i}.name")
        probability = store.get(f"settlement.{i}.cut_off_probability")
        if name is None or probability is None:
            continue
        facts += [name, probability]
        items.append(i18n["labels"]["settlement_probability"].format(
            name=name.formatted(lang), probability=probability.formatted(lang)))
    slots["settlement_list"] = _text_slot("settlement_list", _join(items, i18n), facts)
    return ToolResult(tool="cutoff_settlements", template_key="cutoff_settlements",
                      slots=slots,
                      caveat_fact_ids=("cutoff.caveat", "cutoff.probability_meaning",
                                       "osm.snapshot_date"))


class UnknownSettlement(LookupError):
    """A question named a settlement that is not in this run's layer."""

    def __init__(self, query: str):
        super().__init__(query)
        self.query = query


def settlement_status(store: EvidenceStore, lang: str, i18n, *, name: str) -> ToolResult:
    store.need("cutoff")
    matches = store.find_settlements(name)
    if not matches:
        raise UnknownSettlement(name)
    gated = _slot(store, "cutoff.gated_definition", "gated_definition", lang, "cutoff")
    if len(matches) > 1:
        items, facts = [], []
        for i in matches[:MAX_LIST]:
            name_fact = store.get(f"settlement.{i}.name")
            status_fact = store.get(f"settlement.{i}.status")
            probability = store.get(f"settlement.{i}.cut_off_probability")
            if name_fact is None or status_fact is None or probability is None:
                continue
            facts += [name_fact, status_fact, probability]
            items.append(i18n["labels"]["settlement_status_item"].format(
                name=name_fact.formatted(lang), status=status_fact.formatted(lang),
                probability=probability.formatted(lang)))
        count = store.derive(f"query.{slug(name)}.match_count", len(matches), "count",
                             inputs=tuple(f"settlement.{i}.name" for i in matches),
                             derivation="settlements in this run whose name matches the "
                                        "one asked about",
                             source_file="settlements.geojson")
        slots = {
            "match_count": Slot("match_count", count.formatted(lang), count),
            "settlement_name": _text_slot("settlement_name", str(name)),
            "match_list": _text_slot("match_list", _join(items, i18n), facts),
            "gated_definition": gated,
        }
        return ToolResult(tool="settlement_status", template_key="settlement_status_many",
                          slots=slots, caveat_fact_ids=("cutoff.caveat",
                                                        "cutoff.probability_meaning"))

    index = matches[0]
    base = f"settlement.{index}"
    key = f"{base}.{CUTOFF_GATE_GRAPH}_{CUTOFF_GATE_TARGET}_{CUTOFF_GATE_SCENARIO}"
    slots = {
        "settlement_name": _slot(store, f"{base}.name", "settlement_name", lang, "cutoff"),
        "settlement_status": _slot(store, f"{base}.status", "settlement_status", lang,
                                   "cutoff"),
        "cut_off_probability": _slot(store, f"{base}.cut_off_probability",
                                     "cut_off_probability", lang, "cutoff"),
        "gated_definition": gated,
    }
    before = store.get(f"{base}.{CUTOFF_GATE_GRAPH}_{CUTOFF_GATE_TARGET}_before_km")
    after = store.get(f"{key}_after_km")
    if before is None or after is None:
        return ToolResult(tool="settlement_status",
                          template_key="settlement_status_no_route", slots=slots,
                          caveat_fact_ids=("cutoff.caveat", "cutoff.probability_meaning"))
    slots["before_km"] = Slot("before_km", before.formatted(lang), before)
    slots["after_km"] = Slot("after_km", after.formatted(lang), after)
    return ToolResult(tool="settlement_status", template_key="settlement_status", slots=slots,
                      caveat_fact_ids=("cutoff.caveat", "cutoff.probability_meaning"))


def route_to(store: EvidenceStore, lang: str, i18n, *, name: str,
             target: str = CUTOFF_GATE_TARGET, graph: str = CUTOFF_GATE_GRAPH,
             scenario: str = CUTOFF_GATE_SCENARIO) -> ToolResult:
    store.need("cutoff")
    matches = store.find_settlements(name)
    if not matches:
        raise UnknownSettlement(name)
    if len(matches) > 1:
        # Several settlements share this name. Routing from whichever came
        # first in the layer would answer confidently about a village the
        # reader did not mean, so say so instead - the same answer
        # settlement_status gives, from the same facts.
        return settlement_status(store, lang, i18n, name=name)
    index = matches[0]
    base = f"settlement.{index}"
    key = f"{base}.{graph}_{target}_{scenario}"
    slots = {
        "settlement_name": _slot(store, f"{base}.name", "settlement_name", lang, "cutoff"),
        "target": _text_slot("target", i18n["labels"][target]),
        "graph": _text_slot("graph", i18n["labels"][graph]),
        "route_status": _slot(store, f"{key}_status", "route_status", lang, "cutoff"),
        "before_km": _slot(store, f"{base}.{graph}_{target}_before_km", "before_km", lang,
                           "cutoff", required=False),
    }
    after = store.get(f"{key}_after_km")
    if after is None:
        return ToolResult(tool="route_to", template_key="route_to_cut", slots=slots,
                          caveat_fact_ids=("cutoff.caveat",))
    for name_, fact_id in (("after_km", f"{key}_after_km"),
                           ("detour_km", f"{key}_detour_km"),
                           ("detour_factor", f"{key}_detour_factor"),
                           ("detour_pct", f"{key}_detour_pct")):
        slots[name_] = _slot(store, fact_id, name_, lang, "cutoff", required=False)
    return ToolResult(tool="route_to", template_key="route_to", slots=slots,
                      caveat_fact_ids=("cutoff.caveat",))


def lost_access(store: EvidenceStore, lang: str, i18n, *,
                target: str = CUTOFF_GATE_TARGET, graph: str = CUTOFF_GATE_GRAPH,
                scenario: str = CUTOFF_GATE_SCENARIO, top: int = 5) -> ToolResult:
    """Who lost a route to the nearest destination, and by how much the rest
    were lengthened. The multi-part question ("which villages lost hospital
    access, and how much longer is the detour?") in one tool, because both
    halves come from the same per-settlement facts."""
    store.need("cutoff")
    lost, detoured = [], []
    for i in range(store.settlement_count()):
        status = store.get(f"settlement.{i}.{graph}_{target}_{scenario}_status")
        if status is None:
            continue
        if status.value == STATUS_CUT_OFF:
            lost.append(i)
        elif status.value == STATUS_DETOUR:
            detour = store.get(f"settlement.{i}.{graph}_{target}_{scenario}_detour_km")
            detoured.append((i, _number(detour.value) if detour is not None else None))

    status_inputs = tuple(f"settlement.{i}.{graph}_{target}_{scenario}_status"
                          for i in range(store.settlement_count()))
    lost_count = store.derive(f"lost_access.{graph}_{target}_{scenario}.lost_count",
                              len(lost), "count", inputs=status_inputs,
                              derivation=f"settlements with no {graph} route to the "
                                         f"nearest {target} ({scenario})",
                              source_file="settlements.geojson")
    detour_count = store.derive(f"lost_access.{graph}_{target}_{scenario}.detour_count",
                                len(detoured), "count", inputs=status_inputs,
                                derivation=f"settlements that keep a {graph} route to the "
                                           f"nearest {target} but a longer one ({scenario})",
                                source_file="settlements.geojson")
    limit = max(0, min(int(top), MAX_LIST))
    lost_items, lost_facts = [], []
    for i in lost[:limit]:
        fact = store.get(f"settlement.{i}.name")
        if fact is not None:
            lost_facts.append(fact)
            lost_items.append(fact.formatted(lang))
    detoured.sort(key=lambda row: (-(row[1] if row[1] is not None else -1.0), row[0]))
    detour_items, detour_facts = [], []
    for i, _ in detoured[:limit]:
        name_fact = store.get(f"settlement.{i}.name")
        km_fact = store.get(f"settlement.{i}.{graph}_{target}_{scenario}_detour_km")
        if name_fact is None or km_fact is None:
            continue
        detour_facts += [name_fact, km_fact]
        detour_items.append(i18n["labels"]["settlement_detour"].format(
            name=name_fact.formatted(lang), detour_km=km_fact.formatted(lang)))

    slots = {
        "lost_count": Slot("lost_count", lost_count.formatted(lang), lost_count),
        "detour_count": Slot("detour_count", detour_count.formatted(lang), detour_count),
        "lost_list": _text_slot("lost_list", _join(lost_items, i18n), lost_facts),
        "detour_list": _text_slot("detour_list", _join(detour_items, i18n), detour_facts),
        "target": _text_slot("target", i18n["labels"][target]),
        "graph": _text_slot("graph", i18n["labels"][graph]),
    }
    key = "lost_access" if lost else "lost_access_none"
    if key == "lost_access_none":
        slots = {k: v for k, v in slots.items() if k in
                 ("detour_count", "target", "graph")}
    return ToolResult(tool="lost_access", template_key=key, slots=slots,
                      caveat_fact_ids=("cutoff.caveat", "cutoff.probability_meaning",
                                       "osm.snapshot_date"))


# --- limitations ------------------------------------------------------------


def limitations(store: EvidenceStore, lang: str, i18n) -> ToolResult:
    """What this map cannot say. Never refuses: a run that is missing a layer
    still has limitations worth stating, and the missing layer IS one."""
    pairs = [
        ("sensors_used", "sensor.sensors_used"),
        ("post_pass_date", "sensor.post_pass_date"),
        ("post_latency_days", "sensor.post_latency_days"),
        ("event_date", "run.event_date"),
        ("relative_orbit", "sensor.relative_orbit"),
        ("direction", "sensor.direction"),
        ("pre_pass_count", "sensor.pre_pass_count"),
        ("optical_status", "optical.status"),
        ("not_analysed_pct", "flood.not_analysed_pct"),
        ("flood_in_narrow_valley_pct", "reliability.flood_in_narrow_valley_pct"),
        ("narrow_valley_caveat", "reliability.caveat"),
        ("osm_snapshot_date", "osm.snapshot_date"),
    ]
    slots = {name: _slot(store, fact_id, name, lang, required=False, i18n=i18n)
             for name, fact_id in pairs}
    # The narrow-valley caveat has ONE source string (config.NARROW_VALLEY_CAVEAT)
    # and one translation of it per language, so the dashboard, the sitrep and
    # this answer cannot word it three different ways. The fact stays attached,
    # so the trace still points at where the caveat came from.
    caveat = store.get("reliability.caveat")
    slots["narrow_valley_caveat"] = Slot("narrow_valley_caveat",
                                         i18n["labels"]["caveat_narrow_valley"],
                                         caveat)
    return ToolResult(tool="limitations", template_key="limitations", slots=slots)


# --- the registry -----------------------------------------------------------

TOOLS = {
    "flooded_area": ToolSpec(
        name="flooded_area", capability="flood",
        description="How much flood water this run mapped, split by confidence class, "
                    "with permanent water and the analysed area beside it.",
        parameters=(Parameter("by_confidence", "bool",
                              "split the area into high and medium confidence",
                              default=True),),
        examples=("how much area is flooded?", "flooded area by confidence")),
    "debris": ToolSpec(
        name="debris", capability="flood",
        description="How much POSSIBLE debris or deposit was mapped. Always reported as "
                    "unverified.",
        examples=("how much debris is there?", "any landslide deposits?")),
    "damaged_buildings": ToolSpec(
        name="damaged_buildings", capability="damage",
        description="How many OpenStreetMap buildings fall inside the flood extent.",
        examples=("how many buildings are damaged?",)),
    "cut_roads": ToolSpec(
        name="cut_roads", capability="damage",
        description="Which road segments are cut by the flood, with the cut and flooded "
                    "lengths.",
        parameters=(Parameter("top", "int", "how many roads to name", default=5,
                              minimum=0, maximum=MAX_LIST),),
        examples=("which roads are cut?", "how many km of road is flooded?")),
    "bridges_out": ToolSpec(
        name="bridges_out", capability="damage",
        description="Which bridges are likely out, always labelled unverified.",
        parameters=(Parameter("top", "int", "how many bridges to name", default=5,
                              minimum=0, maximum=MAX_LIST),),
        examples=("which bridges are out?",)),
    "cutoff_settlements": ToolSpec(
        name="cutoff_settlements", capability="cutoff",
        description="Which settlements have no vehicle route left to their nearest health "
                    "facility, with each one's cut-off probability.",
        parameters=(Parameter("top", "int", "how many settlements to name", default=5,
                              minimum=0, maximum=MAX_LIST),
                    Parameter("min_probability", "float",
                              "only settlements at or above this probability", default=0.0,
                              minimum=0.0, maximum=1.0)),
        examples=("which villages are cut off?", "who is isolated?")),
    "settlement_status": ToolSpec(
        name="settlement_status", capability="cutoff",
        description="The status of ONE named settlement: cut off, detour, reachable or no "
                    "route before the flood, with its probability and route lengths.",
        parameters=(Parameter("name", "str", "the settlement's name, as asked",
                              required=True),),
        examples=("is Khanigaun cut off?",)),
    "route_to": ToolSpec(
        name="route_to", capability="cutoff",
        description="The route from a named settlement to its nearest health facility or "
                    "town, before and after the flood, and how much longer it became.",
        parameters=(Parameter("name", "str", "the settlement's name", required=True),
                    Parameter("target", "str", "where it routes to",
                              default=CUTOFF_GATE_TARGET, choices=CUTOFF_TARGETS),
                    Parameter("graph", "str", "by vehicle or on foot",
                              default=CUTOFF_GATE_GRAPH, choices=GRAPH_KINDS),
                    Parameter("scenario", "str", "bridges assumed intact or failed",
                              default=CUTOFF_GATE_SCENARIO, choices=SCENARIOS)),
        examples=("how far is the hospital from Khanigaun now?",)),
    "lost_access": ToolSpec(
        name="lost_access", capability="cutoff",
        description="Which settlements LOST their route to the nearest health facility or "
                    "town, and how much longer the remaining detours are. Use this for a "
                    "question that asks both at once.",
        parameters=(Parameter("target", "str", "where they route to",
                              default=CUTOFF_GATE_TARGET, choices=CUTOFF_TARGETS),
                    Parameter("graph", "str", "by vehicle or on foot",
                              default=CUTOFF_GATE_GRAPH, choices=GRAPH_KINDS),
                    Parameter("scenario", "str", "bridges assumed intact or failed",
                              default=CUTOFF_GATE_SCENARIO, choices=SCENARIOS),
                    Parameter("top", "int", "how many settlements to name", default=5,
                              minimum=0, maximum=MAX_LIST)),
        examples=("which villages lost hospital access and how much longer is the detour?",)),
    "limitations": ToolSpec(
        name="limitations", capability="flood",
        description="What this map cannot say: which sensor and pass date, radar-only or "
                    "optical too, the share not analysed, cloud, and the OSM snapshot date.",
        examples=("what are the limitations?", "how reliable is this?")),
}

IMPLEMENTATIONS = {
    "flooded_area": flooded_area,
    "debris": debris,
    "damaged_buildings": damaged_buildings,
    "cut_roads": cut_roads,
    "bridges_out": bridges_out,
    "cutoff_settlements": cutoff_settlements,
    "settlement_status": settlement_status,
    "route_to": route_to,
    "lost_access": lost_access,
    "limitations": limitations,
}

assert set(TOOLS) == set(IMPLEMENTATIONS), "a tool is declared without an implementation"
