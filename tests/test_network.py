"""The cut-off analysis on a hand-made network whose answers are known by hand.

SYNTHETIC: every road, settlement and flood pixel below is written out here, so
nothing in this file is real-world cut-off accuracy (Rule 3.8). The real
numbers come from `eval/cutoff_eval.py` into `eval/results/cutoff.csv`.

THE FIXTURE, drawn to scale (100 x 100 cells at 20 m, so 2000 m square):

    y=1800   NORTH ROAD   ============================================
                          |                    |                     |
    y=1200 - 800          |              ##### |                     |
             FLOOD        |              ##### |  (x 900-1100 only,  |
             BAND         |              ##### |   y 800-1200 only)  |
                          |                    |                     |
    y=200    SOUTH ROAD   ============================================
                          x=200              x=1000               x=1800

  * `west` settlement at (200, 1800) and `east` at (1800, 1800), both north.
  * `south` settlement at (1000, 200), on the south road; `market town` at
    (1800, 200), which is also where the one health facility is.
  * the ONLY north-south links are three vertical roads at x = 200, 1000, 1800.

The band is bounded in BOTH directions on purpose: it crosses the middle link
and nothing else. The two east-west roads run outside it (y = 1800 and y = 200),
so with all three links present nobody is cut off - which is what makes the
"remove the dry links" cases below mean something.

The change score is |z| = 6 inside the band and 0 outside, i.e. what a real
detection looks like: a strict threshold in the ensemble must not be able to
empty the extent, or the probabilities would measure the fixture rather than
the code.
"""

from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

from strandline import damage, network
from strandline.config import (
    CLASS_FLOOD_MEDIUM,
    CLASS_NO_CHANGE,
    GRAPH_FOOT,
    GRAPH_VEHICLE,
    RELIABILITY_NARROW_VALLEY,
    SCENARIO_BRIDGES_FAILED,
    SCENARIO_BRIDGES_INTACT,
    STATUS_CUT_OFF,
    STATUS_DETOUR,
    STATUS_NO_BASELINE,
    STATUS_REACHABLE,
    DamageParams,
    ExtentParams,
    MonteCarloParams,
    NetworkParams,
)

RES = 20.0
N = 100
CRS = "EPSG:32633"


class Grid:
    def __init__(self):
        self.res = RES
        self.crs = CRS
        self.width = self.height = N
        self.transform = from_origin(0.0, N * RES, RES, RES)


GRID = Grid()


def classes_with_strip(x0=900.0, x1=1100.0, y0=800.0, y1=1200.0):
    """A flood band bounded in x AND y. Rows run north (y high) to south."""
    arr = np.full((N, N), CLASS_NO_CHANGE, dtype=np.uint8)
    row_top, row_bottom = int((N * RES - y1) / RES), int((N * RES - y0) / RES)
    arr[row_top:row_bottom, int(x0 / RES):int(x1 / RES)] = CLASS_FLOOD_MEDIUM
    return arr


def strong_zscore(classes):
    """|z| = 6 where the detector called flood, 0 elsewhere.

    A real flood pixel has a change score well past the seed; a fixture of
    zeros would let any stricter ensemble threshold erase the extent, and the
    probabilities would then be a property of the fixture.
    """
    return np.where(classes == CLASS_FLOOD_MEDIUM, 6.0, 0.0)


def fc(geoms, props):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": p,
         "geometry": json.loads(json.dumps(g.__geo_interface__))}
        for g, p in zip(geoms, props)]}


def road_layer(links=(200, 1000, 1800), bridge_at=()):
    geoms = [LineString([(100, 1800), (1900, 1800)]),
             LineString([(100, 200), (1900, 200)])]
    props = [{"@osmId": "way/north", "highway": "secondary"},
             {"@osmId": "way/south", "highway": "secondary"}]
    for x in links:
        geoms.append(LineString([(x, 200), (x, 1800)]))
        tags = {"@osmId": f"way/link{x}", "highway": "tertiary"}
        if x in bridge_at:
            tags["bridge"] = "yes"
        props.append(tags)
    return fc(geoms, props)


PLACES = fc([Point(200, 1800), Point(1800, 1800), Point(1000, 200), Point(1800, 200)],
            [{"@osmId": "node/w", "place": "village", "name": "west"},
             {"@osmId": "node/e", "place": "village", "name": "east"},
             {"@osmId": "node/s", "place": "village", "name": "south"},
             {"@osmId": "node/t", "place": "town", "name": "market town"}])

HEALTH = fc([Point(1800, 200)], [{"@osmId": "node/h", "amenity": "clinic"}])
NO_BUILDINGS = {"type": "FeatureCollection", "features": []}


@pytest.fixture(autouse=True)
def _grid_crs(monkeypatch):
    """The fixture geometry is already in the grid CRS, so skip reprojection.

    Coordinates are metres, which is what makes every expected answer below
    checkable by hand; `assess` normally reprojects from WGS84.
    """
    from strandline import osm as osm_mod

    def fake(body, crs_to=None):
        features = list((body or {}).get("features") or [])
        if not features:
            return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=CRS)
        return gpd.GeoDataFrame.from_features(features, crs=CRS)

    monkeypatch.setattr(osm_mod, "to_geodataframe", fake)


def run(classes=None, links=(200, 1000, 1800), bridge_at=(), buildings=None,
        mc=None, places=PLACES, health=HEALTH, roads=None, **kwargs):
    classes = classes if classes is not None else classes_with_strip()
    layers = {"roads": roads if roads is not None else road_layer(links, bridge_at),
              "places": places, "health": health,
              "buildings": buildings or NO_BUILDINGS,
              "waterways": NO_BUILDINGS, "bridges": NO_BUILDINGS}
    return network.assess(
        classes, strong_zscore(classes), None, GRID, layers, z_seed=3.0,
        mc=mc or MonteCarloParams(n_runs=4), log=lambda *a: None, **kwargs)


# --- the graph ----------------------------------------------------------------

def test_the_two_graphs_differ_exactly_by_the_foot_only_types():
    vehicle = set(network.highway_types(GRAPH_VEHICLE))
    foot = set(network.highway_types(GRAPH_FOOT))
    assert vehicle < foot
    assert "track" in foot and "track" not in vehicle
    assert "footway" in foot and "footway" not in vehicle
    assert "primary" in vehicle


def test_a_track_is_in_the_foot_graph_only():
    layers = {"roads": fc([LineString([(100, 100), (900, 100)])],
                          [{"@osmId": "way/t", "highway": "track"}]),
              "places": PLACES, "health": HEALTH,
              "buildings": NO_BUILDINGS, "waterways": NO_BUILDINGS,
              "bridges": NO_BUILDINGS}
    segments = damage.split_at_junctions(
        __import__("strandline").osm.to_geodataframe(layers["roads"], CRS))
    assert network.build_graph(segments, GRID, GRAPH_VEHICLE).n_edges == 0
    assert network.build_graph(segments, GRID, GRAPH_FOOT).n_edges == 1


def test_junctions_become_shared_nodes_so_the_network_is_connected():
    """Three crossing roads must give ONE component, not five lines."""
    result = run()
    graph = result.graphs[GRAPH_VEHICLE]
    from scipy.sparse.csgraph import connected_components

    n, _labels = connected_components(network._matrix(
        graph, np.zeros(graph.n_edges, dtype=bool)), directed=False)
    # Every node is in one component; isolated nodes would raise the count.
    assert n == 1, f"{n} components: junctions did not snap into shared nodes"


def test_edge_length_is_metres_on_the_grid():
    graph = run().graphs[GRAPH_VEHICLE]
    assert graph.total_length_m() == pytest.approx(2 * 1800 + 3 * 1600, abs=1.0)


# --- the rasterised flood test agrees with the geometry one -------------------

def test_the_rasterised_edge_test_agrees_with_the_damage_geometry_test():
    """One definition of "the flood is on this road", two implementations.

    `damage` intersects the polygon; this module samples pixels so that 200
    ensemble members are affordable. If they disagreed, a settlement's status
    and its probability would come from different definitions.
    """
    classes = classes_with_strip()
    result = run(classes)
    graph = result.graphs[GRAPH_VEHICLE]
    flat = network.flood_flat_mask(classes, GRID, ExtentParams(), DamageParams().buffer_px)
    rasterised = graph.flooded_edges(flat)

    geom = damage.extent_geometry(classes, GRID, ExtentParams(), DamageParams().buffer_px)
    from shapely.geometry import LineString as LS

    for i in range(graph.n_edges):
        u, v = graph.nodes[graph.edge_u[i]], graph.nodes[graph.edge_v[i]]
        by_geometry = LS([tuple(u), tuple(v)]).intersection(geom).length > 0
        assert bool(rasterised[i]) == by_geometry, (
            f"edge {graph.edge_segment_id[i]} disagrees: rasterised "
            f"{rasterised[i]}, geometry {by_geometry}")


# --- statuses, each computed by hand ------------------------------------------

def _status(result, name, graph=GRAPH_VEHICLE, target="health",
            scenario=SCENARIO_BRIDGES_INTACT):
    i = result.settlements.names.index(name)
    return result.status[(graph, target, scenario)][i]


def test_with_two_dry_links_nobody_is_cut_off():
    """The flood cuts the middle link only; the outer two still cross."""
    result = run()
    for name in ("west", "east", "south"):
        assert _status(result, name) in (STATUS_REACHABLE, STATUS_DETOUR), name
    assert result.cut_off_names() == []


def test_removing_every_dry_link_cuts_the_northern_settlements_off():
    """Only the flooded middle link remains, so north cannot reach the clinic."""
    result = run(links=(1000,))
    assert _status(result, "west") == STATUS_CUT_OFF
    assert _status(result, "east") == STATUS_CUT_OFF
    # The clinic is ON the south road, so `south` keeps its route.
    assert _status(result, "south") == STATUS_REACHABLE
    assert sorted(result.cut_off_names()) == ["east", "west"]


def test_a_longer_way_round_is_a_detour_not_a_cut_off():
    """West loses its own link and must go round the far side.

    With the clinic at the south-WEST corner (200, 200), west's baseline is
    straight down its own link: 1600 m. Flood that link, leave only the far
    x=1800 one, and the way round is 1600 east + 1600 south + 1600 back west =
    4800 m. 4800 > 1.5 x 1600, so this is a DETOUR and not a cut-off - the
    distinction the detour_factor exists to draw.
    """
    clinic_sw = fc([Point(200, 200)], [{"@osmId": "node/h", "amenity": "clinic"}])
    classes = classes_with_strip(x0=100.0, x1=300.0)   # over the x=200 link
    result = run(classes, links=(200, 1800), health=clinic_sw)
    assert _status(result, "west") == STATUS_DETOUR
    i = result.settlements.names.index("west")
    node = result.attached[GRAPH_VEHICLE][i]
    before = result.before_m[(GRAPH_VEHICLE, "health")][node]
    after = result.after_m[(GRAPH_VEHICLE, "health", SCENARIO_BRIDGES_INTACT)][node]
    assert before == pytest.approx(1600.0, abs=1.0)
    assert after == pytest.approx(4800.0, abs=1.0)
    assert after > NetworkParams().detour_factor * before


def test_a_settlement_with_no_mapped_road_is_not_called_cut_off():
    """The distinction that stops a mapping gap becoming a flood impact.

    The hamlet sits at (100, 1000), more than `settlement_link_max_m` from any
    node of the graph (the nearest are the link ends at (200, 200) and
    (200, 1800), both ~806 m away). It is reported as having no baseline route,
    NOT as cut off, and it is excluded from the gated cut-off set.
    """
    far = fc([Point(200, 1800), Point(100, 1000)],
             [{"@osmId": "node/w", "place": "village", "name": "west"},
              {"@osmId": "node/x", "place": "hamlet", "name": "unmapped hamlet"}])
    result = run(places=far)
    assert _status(result, "unmapped hamlet") == STATUS_NO_BASELINE
    assert "unmapped hamlet" not in result.cut_off_names()
    # And the limitation is COUNTED, not hidden: nearest-node attachment can
    # miss a settlement beside the middle of a long road, so every run reports
    # how many settlements it could not attach at all.
    assert result.report()["settlements"]["not_attached_to_any_graph"] == 1


def test_a_settlement_that_had_no_route_before_the_flood_is_not_cut_off():
    """No clinic anywhere: nobody has a baseline, so nobody is cut off."""
    result = run(health={"type": "FeatureCollection", "features": []})
    for name in ("west", "east", "south"):
        assert _status(result, name) == STATUS_NO_BASELINE
    assert result.cut_off_names() == []


def test_the_foot_graph_can_be_reachable_where_the_vehicle_graph_is_cut_off():
    """A footpath across the valley is the difference between the two answers.

    The only vehicle link left is the flooded middle one, so an ambulance
    cannot get to west. A dry footpath down x=200 means a person can walk.
    """
    roads = road_layer(links=(1000,))
    roads["features"].append({
        "type": "Feature",
        "properties": {"@osmId": "way/path", "highway": "path"},
        "geometry": json.loads(json.dumps(
            LineString([(200, 1800), (200, 200)]).__geo_interface__))})
    result = run(roads=roads, mc=MonteCarloParams(n_runs=2))
    assert _status(result, "west", graph=GRAPH_VEHICLE) == STATUS_CUT_OFF
    assert _status(result, "west", graph=GRAPH_FOOT) in (STATUS_REACHABLE, STATUS_DETOUR)


def test_the_town_destination_is_routed_as_well_as_the_clinic():
    result = run(links=(1000,))
    assert _status(result, "west", target="town") == STATUS_CUT_OFF
    assert (GRAPH_VEHICLE, "town") in result.before_m


# --- the bridges-failed scenario ----------------------------------------------

def test_a_failed_bridge_cuts_a_settlement_the_intact_scenario_does_not():
    """The two scenarios must be able to differ, or the second is decoration."""
    result = run(links=(200, 1000), bridge_at=(200,))
    assert _status(result, "west", scenario=SCENARIO_BRIDGES_INTACT) in (
        STATUS_REACHABLE, STATUS_DETOUR)
    assert _status(result, "west", scenario=SCENARIO_BRIDGES_FAILED) == STATUS_CUT_OFF


def test_only_the_flagged_bridges_fail_when_a_flagged_set_is_given():
    """"Bridges failed" means the crossings we doubt, not every bridge."""
    intact = run(links=(200, 1000), bridge_at=(200,), bridges_out_ids=set())
    assert _status(intact, "west", scenario=SCENARIO_BRIDGES_FAILED) in (
        STATUS_REACHABLE, STATUS_DETOUR)
    flagged = run(links=(200, 1000), bridge_at=(200,),
                  bridges_out_ids={"way/link200"})
    assert _status(flagged, "west", scenario=SCENARIO_BRIDGES_FAILED) == STATUS_CUT_OFF


# --- settlements ---------------------------------------------------------------

def test_building_clusters_become_settlements_where_osm_names_none():
    cluster = [box(1500 + 30 * i, 1500, 1510 + 30 * i, 1510) for i in range(6)]
    buildings = fc(cluster, [{"@osmId": f"way/b{i}", "building": "house"}
                             for i in range(6)])
    result = run(buildings=buildings, places=fc([Point(200, 1800)], [
        {"@osmId": "node/w", "place": "village", "name": "west"}]))
    sources = result.settlements.sources
    assert "building_cluster" in sources
    i = sources.index("building_cluster")
    assert result.settlements.n_buildings[i] == 6


def test_a_scatter_of_isolated_buildings_is_not_a_settlement():
    far_apart = [box(100 + 600 * i, 100, 110 + 600 * i, 110) for i in range(3)]
    buildings = fc(far_apart, [{"@osmId": f"way/b{i}"} for i in range(3)])
    result = run(buildings=buildings)
    assert "building_cluster" not in result.settlements.sources


def test_a_cluster_on_top_of_an_osm_place_node_is_not_counted_twice():
    cluster = [box(200 + 20 * i, 1800, 210 + 20 * i, 1810) for i in range(8)]
    buildings = fc(cluster, [{"@osmId": f"way/b{i}"} for i in range(8)])
    result = run(buildings=buildings)
    assert result.settlements.sources.count("building_cluster") == 0
    assert "west" in result.settlements.names


# --- DBSCAN --------------------------------------------------------------------

def test_dbscan_finds_two_clusters_and_leaves_noise_unlabelled():
    a = np.array([[0.0, 0.0], [10, 0], [0, 10], [10, 10], [5, 5]])
    b = a + 1000.0
    noise = np.array([[500.0, 500.0]])
    labels = network.dbscan(np.vstack([a, b, noise]), eps=50.0, min_samples=4)
    assert sorted(set(labels)) == [-1, 0, 1]
    assert labels[-1] == -1
    assert len(set(labels[:5])) == 1 and len(set(labels[5:10])) == 1


def test_dbscan_is_deterministic_and_independent_of_nothing_but_its_inputs():
    points = np.array([[0.0, 0.0], [5, 0], [0, 5], [5, 5], [2, 2]])
    first = network.dbscan(points, 20.0, 3)
    second = network.dbscan(points, 20.0, 3)
    assert np.array_equal(first, second)


def test_dbscan_on_no_points_is_empty_not_an_error():
    assert len(network.dbscan(np.zeros((0, 2)), 10.0, 3)) == 0


# --- the ensemble --------------------------------------------------------------

def test_the_same_seed_gives_identical_output():
    """Rule 5. The headline statuses AND every probability must match."""
    mc = MonteCarloParams(n_runs=12, seed=7)
    first = run(mc=mc)
    second = run(mc=mc)
    assert np.array_equal(first.probability, second.probability)
    assert first.gate_status() == second.gate_status()
    assert first.report()["probability"] == second.report()["probability"]


def test_a_different_seed_is_allowed_to_differ_but_the_statuses_are_not():
    """The deterministic answer must not depend on the ensemble's seed."""
    a = run(mc=MonteCarloParams(n_runs=12, seed=1))
    b = run(mc=MonteCarloParams(n_runs=12, seed=2))
    assert a.gate_status() == b.gate_status()


def test_probabilities_are_shares_in_zero_to_one():
    result = run(mc=MonteCarloParams(n_runs=8))
    assert len(result.probability) == len(result.settlements)
    assert np.all((result.probability >= 0.0) & (result.probability <= 1.0))


def test_a_settlement_cut_off_in_every_member_has_probability_one():
    result = run(links=(1000,), mc=MonteCarloParams(n_runs=8))
    i = result.settlements.names.index("west")
    assert result.probability[i] == 1.0


def test_zero_perturbation_reproduces_the_configured_extent_exactly():
    """The ensemble must CONTAIN the answer the run actually gave.

    An ensemble built by re-deriving the extent from the change score alone
    would drop the magnitude gate, the hysteresis, the minimum mapping unit and
    the drainage test, so at zero perturbation it would not reproduce the real
    map - and could not say how stable the real map is.
    """
    classes = classes_with_strip()
    z = np.zeros((N, N))
    base = network.flood_flat_mask(classes, GRID, ExtentParams(), 1)
    same = network.perturbed_flat_mask(classes, z, None, 3.0, 0.0, 1, None,
                                       ExtentParams())
    assert np.array_equal(base, same)


def test_a_looser_threshold_can_only_grow_the_extent_and_a_stricter_one_shrink_it():
    classes = classes_with_strip()
    z = np.zeros((N, N))
    z[:, 40:50] = 2.8        # just under the chosen seed of 3.0
    base = network.perturbed_flat_mask(classes, z, None, 3.0, 0.0, 0, None)
    looser = network.perturbed_flat_mask(classes, z, None, 3.0, -0.5, 0, None)
    stricter = network.perturbed_flat_mask(classes, z, None, 3.0, 0.5, 0, None)
    assert looser.sum() > base.sum(), "a lowered threshold found nothing new"
    assert np.all(base[looser == False] == False)  # noqa: E712 - growth only
    assert stricter.sum() <= base.sum()


def test_low_confidence_narrow_valley_pixels_enter_on_the_coin_flip():
    classes = classes_with_strip()
    reliability = np.zeros((N, N), dtype=np.uint8)
    reliability[:, 60:70] = RELIABILITY_NARROW_VALLEY
    flips = np.zeros((N, N), dtype=bool)
    flips[:, 60:70] = True
    without = network.perturbed_flat_mask(classes, np.zeros((N, N)), reliability,
                                          3.0, 0.0, 0, None)
    with_flip = network.perturbed_flat_mask(classes, np.zeros((N, N)), reliability,
                                            3.0, 0.0, 0, flips)
    assert with_flip.sum() > without.sum()
    assert with_flip.sum() - without.sum() == 10 * N


# --- refusals ------------------------------------------------------------------

def test_no_roads_is_a_refusal_naming_what_to_report():
    with pytest.raises(network.NetworkError) as err:
        run(roads={"type": "FeatureCollection", "features": []})
    assert "NO roads" in str(err.value)
    assert "NOT AVAILABLE" in str(err.value)


def test_no_settlements_is_a_refusal_that_blames_the_data_not_the_flood():
    empty = {"type": "FeatureCollection", "features": []}
    with pytest.raises(network.NetworkError) as err:
        run(places=empty, buildings=empty)
    message = str(err.value)
    assert "NO settlements" in message
    assert "completeness limit, not a finding that nobody is isolated" in message


# --- outputs -------------------------------------------------------------------

def test_the_two_layers_are_written_with_the_caveat_and_both_scenarios(tmp_path):
    result = run(links=(1000,), mc=MonteCarloParams(n_runs=4))
    written = network.write_layers(tmp_path, result, GRID, log=lambda *a: None)
    assert written["settlements"] == len(result.settlements)
    for name in ("settlements", "routes"):
        body = json.loads((tmp_path / f"{name}.geojson").read_text(encoding="utf-8"))
        assert body["type"] == "FeatureCollection"
        assert "sensitivity measure" in body["properties"]["caveat"]
        assert body["properties"]["gated_definition"]["graph"] == GRAPH_VEHICLE
    settle = json.loads((tmp_path / "settlements.geojson").read_text(encoding="utf-8"))
    props = settle["features"][0]["properties"]
    assert "cut_off_probability" in props and "status" in props
    assert "vehicle_health_bridges_intact_status" in props
    assert "foot_health_bridges_failed_status" in props
    assert "vehicle_health_before_m" in props
    assert "NOT a calibrated chance" in props["probability_is"]


def test_the_report_states_the_definition_and_that_the_probability_is_sensitivity():
    report = run().report()
    assert report["definition"]["gated_definition"].startswith("graph vehicle")
    assert "NOT a flood impact" in report["definition"]["no_route_before_the_flood"]
    assert "not a calibrated probability" in report["probability"]["what_it_is"]
    assert report["probability"]["runs"] == 4
    assert "sensitivity measure" in report["caveat"]
    assert report["graphs"][GRAPH_VEHICLE]["edges"] > 0
    assert report["settlements"]["total"] == 4


def test_the_report_carries_every_graph_destination_and_scenario():
    counts = run().report()["status_counts"]
    for graph in (GRAPH_VEHICLE, GRAPH_FOOT):
        for target in ("health", "town"):
            for scenario in (SCENARIO_BRIDGES_INTACT, SCENARIO_BRIDGES_FAILED):
                assert scenario in counts[graph][target], (graph, target, scenario)


# --- parameters ----------------------------------------------------------------

def test_the_network_parameters_do_not_touch_the_flood_params_hash():
    from strandline.config import DetectParams, RunParams, params_dict

    sections = params_dict(DetectParams(), RunParams())
    assert "network" not in sections and "monte_carlo" not in sections


@pytest.mark.parametrize("kwargs", [
    {"detour_factor": 1.0}, {"detour_factor": 0.9}, {"node_snap_m": 0.0},
    {"cluster_min_buildings": 1}, {"cluster_eps_m": -1.0},
])
def test_implausible_network_parameters_are_refused(kwargs):
    with pytest.raises(ValueError):
        NetworkParams(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"n_runs": 0}, {"narrow_valley_flood_probability": 1.5},
    {"z_seed_delta": (0.1, 0.5)}, {"buffer_px_choices": ()},
    {"buffer_px_choices": (-1, 0)},
])
def test_implausible_ensemble_parameters_are_refused(kwargs):
    with pytest.raises(ValueError):
        MonteCarloParams(**kwargs)


# --- identity, parallel edges, and the ensemble's two corrected behaviours ----

def test_cut_off_identity_is_position_not_name():
    """OSM place names are not unique; `names` even manufactures duplicates.

    Two settlements both called "unnamed hamlet" must stay two settlements, or
    a comparison of name sets would merge them - which can only turn
    disagreement into agreement. Measured by the 2026-10-13 audit: 15 place
    nodes share one name in the Larkana area.
    """
    collide = fc([Point(200, 1800), Point(1800, 1800)],
                 [{"@osmId": "node/a", "place": "hamlet"},
                  {"@osmId": "node/b", "place": "hamlet"}])
    result = run(links=(1000,), places=collide)
    assert result.settlements.names == ["unnamed hamlet", "unnamed hamlet"]
    assert result.cut_off_indices() == [0, 1]
    assert len(set(result.cut_off_indices())) == 2
    assert len(set(result.cut_off_names())) == 1, "names collapse; indices must not"
    assert result.label(0) != result.label(1)


def test_parallel_edges_take_the_shortest_not_their_sum():
    """`coo_matrix.tocsr` ADDS duplicate entries, so two ways between the same
    junction pair gave 100 + 300 = 400 m where the distance is 100 m. Common in
    OSM (dual carriageways, service loops) and made commoner by node snapping.
    Found by the 2026-10-13 audit.
    """
    graph = network.RoadGraph(
        kind="x", nodes=np.array([[0.0, 0.0], [100.0, 0.0]]),
        edge_u=np.array([0, 0]), edge_v=np.array([1, 1]),
        edge_len=np.array([100.0, 300.0]), edge_is_bridge=np.zeros(2, dtype=bool),
        edge_segment_id=["a", "b"], edge_pixels=np.zeros(0, dtype=np.int64),
        edge_pixel_offsets=np.zeros(3, dtype=np.int64))
    dist = network.distance_to_nearest(graph, np.array([0]), np.zeros(2, dtype=bool))
    assert dist[1] == pytest.approx(100.0)


def test_a_stricter_threshold_does_not_erase_the_hysteresis_grown_band():
    """The detector admits pixels down to z_grow, not z_seed.

    Comparing a stricter ensemble threshold against `z_seed + delta` deleted
    every grown pixel at once for ANY positive delta - a one-sided erosion of
    ~1.5-2.0 z rather than the +-0.5 z the parameter claims, biasing every
    published probability low. Measured by the 2026-10-13 audit; the comparison
    is now against `z_grow + delta`.
    """
    classes = classes_with_strip()
    z = np.where(classes == CLASS_FLOOD_MEDIUM, 2.0, 0.0)   # a grown band: z < z_seed
    base = network.perturbed_flat_mask(classes, z, None, 3.0, 0.0, 0, None,
                                       z_grow=1.5)
    assert base.sum() > 0, "the fixture must have an extent to erode"
    stricter = network.perturbed_flat_mask(classes, z, None, 3.0, 0.1, 0, None,
                                           z_grow=1.5)
    assert stricter.sum() == base.sum(), "a +0.1 z step erased a grown band"
    # Far enough and it does shrink, which is what the perturbation is for.
    gone = network.perturbed_flat_mask(classes, z, None, 3.0, 0.6, 0, None, z_grow=1.5)
    assert gone.sum() == 0


def test_the_ensemble_contains_the_unperturbed_run():
    """`rng.uniform` is continuous, so without an explicit member the ensemble
    contained the real answer with probability zero - while the documentation
    claimed it contained it. Member 0 is now the unperturbed extent."""
    result = run(mc=MonteCarloParams(n_runs=1))
    assert result.ensemble["unperturbed_member_included"] is True
    assert result.ensemble["z_seed_delta_mean_drawn"] == 0.0
    assert result.ensemble["buffer_px_mean_drawn"] == float(DamageParams().buffer_px)
    # With one member, the probability is exactly the deterministic status.
    gate = result.gate_status()
    for i, status in enumerate(gate):
        assert result.probability[i] == (1.0 if status == STATUS_CUT_OFF else 0.0)


def test_every_pixel_an_edge_touches_is_counted_not_merely_sampled():
    """Half-pixel point sampling skipped cells a segment genuinely crossed
    (6,471 of 20,000 random short segments, per the 2026-10-13 audit), so a
    road clipping the flood thinly was called dry. The rasterisation is now a
    supercover and must agree with a geometry intersection exactly.
    """
    from shapely.geometry import box as shbox

    rng = np.random.default_rng(0)
    for _ in range(40):
        p0 = rng.uniform(100, 1900, size=2)
        p1 = p0 + rng.uniform(-80, 80, size=2)
        line = LineString([tuple(p0), tuple(p1)])
        if line.length <= 0:
            continue
        flat = network._sample_pixels(line, GRID)
        got = {(int(f) // N, int(f) % N) for f in flat}
        truth = set()
        for row in range(N):
            for col in range(N):
                cell = shbox(col * RES, (N - row - 1) * RES,
                             (col + 1) * RES, (N - row) * RES)
                if line.intersects(cell) and not line.touches(cell):
                    truth.add((row, col))
        assert truth <= got, f"missed {sorted(truth - got)} for {line.wkt}"
